#!/usr/bin/env python3
"""Enable CFS bandwidth control without moving the GKI ABI.

CONFIG_CFS_BANDWIDTH adds per-cfs_rq state to `struct cfs_rq`:

    int  runtime_enabled;  s64 runtime_remaining;  u64 throttled_pelt_idle;
    u64  throttled_clock;  u64 throttled_clock_pelt; u64 throttled_clock_pelt_time;
    int  throttled;  int throttle_count;  struct list_head throttled_list;

`struct rq` embeds a `struct cfs_rq`, and `struct rq *` appears in the prototypes
of exported symbols - __task_rq_lock, __migrate_task, __balance_callbacks and the
whole __traceiter_android_rvh_* vendor-hook family - so genksyms expands it and
142 CRCs move. That is why a prebuilt Qualcomm perf/thermal/charging module
refuses to load on a kernel with CFS_BANDWIDTH on.

The 142 are all driven by those fields, so they are hoisted here into a
`struct cfs_bw_cpu` block allocated per (task_group, cpu), reached through
`struct cfs_bandwidth`. That struct is NOT in the CRC surface: turning the config
on already embeds a whole `struct cfs_bandwidth` inside `struct task_group`, and
that moved only the 142 rq symbols - so adding a pointer to it is invisible.

Access cost is one extra dereference (tg -> cfs_bandwidth -> cpu block), with the
cpu taken from `rq_of(cfs_rq)`; no hashing, no allocation in any hot path.

Idempotent; anchors are verified and the script exits non-zero if the tree does
not look like it expects. Field names are read from the tree, so the same script
handles trees where the set differs (6.12+ moved throttled_pelt_idle).

Usage: cfs_bw_zero_drift.py <kernel/common>
"""

import os
import re
import sys
from typing import NoReturn

MARKER = "/* cfs bandwidth zero-drift port (GKI_KernelSU_SUSFS) */"

FALLBACK = """
#ifdef CONFIG_CFS_BANDWIDTH
/*
 * Fallback used only if the per-(task_group, cpu) block could not be allocated
 * at group creation. Keeping it per-cpu means no cross-CPU sharing, and the
 * accessor stays branch-only - never NULL.
 */
DECLARE_PER_CPU(struct cfs_bw_cpu, cfs_bw_nostate);

static inline struct cfs_bw_cpu *cfs_rq_bw(struct cfs_rq *cfs_rq)
{
	struct cfs_bandwidth *cfs_b = &cfs_rq->tg->cfs_bandwidth;
	unsigned int cpu = (cfs_rq)->rq->cpu;

	if (unlikely(!cfs_b->cpu_state))
		return &per_cpu(cfs_bw_nostate, cpu);

	return &cfs_b->cpu_state[cpu];
}

/*
 * The throttled list links cfs_bw_cpu entries now, so list_for_each_entry()
 * callers need the owning cfs_rq back. The entry's index in the group's array is
 * its cpu, and the group owns one cfs_rq per cpu.
 */
static inline struct cfs_rq *cfs_bw_to_cfs_rq(struct cfs_bandwidth *cfs_b,
					      struct cfs_bw_cpu *bw)
{
	struct task_group *tg = container_of(cfs_b, struct task_group, cfs_bandwidth);
	unsigned int cpu = bw - cfs_b->cpu_state;

	return tg->cfs_rq[cpu];
}

/* every former cfs_rq-><bw field> goes through here */
#define CFS_BW(cfs_rq)	cfs_rq_bw(cfs_rq)
#endif /* CONFIG_CFS_BANDWIDTH */
"""


def die(msg: str) -> NoReturn:
    sys.exit(f"ERROR: {msg}")


def read(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def write(p: str, t: str) -> None:
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(t)


def field_names(block: str) -> list:
    """Names declared inside the struct block, ignoring nested #if lines."""
    names = []
    for line in block.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "/*", "*", "}")):
            continue
        m = re.match(r"^[\w\s\*]+?\b(\w+)\s*(\[[^\]]*\])?\s*;$", s)
        if m:
            names.append(m.group(1))
    return names


def main() -> None:
    if len(sys.argv) != 2:
        die("usage: cfs_bw_zero_drift.py <kernel/common>")
    root = sys.argv[1].rstrip("/")
    sched_h = os.path.join(root, "kernel/sched/sched.h")
    fair_c = os.path.join(root, "kernel/sched/fair.c")
    for p in (sched_h, fair_c):
        if not os.path.exists(p):
            die(f"{p}: not found")

    print(f"[+] cfs bandwidth zero-drift port: {root}")
    src = read(sched_h)

    if MARKER in src:
        print("  already ported - verifying only")
    else:
        # ---- 1. lift the field block out of struct cfs_rq -------------------
        m = re.search(r"(struct cfs_rq \{.*?\n)(#ifdef CONFIG_CFS_BANDWIDTH\n)(.*?)(#endif /\* CONFIG_CFS_BANDWIDTH \*/\n)(.*?\n\};)",
                      src, re.S)
        if not m:
            die("sched.h: could not locate struct cfs_rq's CONFIG_CFS_BANDWIDTH block")
        head, ifdef, body, endif_, tail = m.groups()
        fields = field_names(body)
        if not fields:
            die("sched.h: no fields parsed out of the CONFIG_CFS_BANDWIDTH block")
        print(f"  sched.h: {len(fields)} field(s) to move: {', '.join(fields)}")

        # the block disappears from struct cfs_rq entirely
        src = src[:m.start()] + head + tail + src[m.end():]

        # ---- 2. define the block as its own struct --------------------------
        newstruct = (f"\n{MARKER}\n"
                     f"/* per-(task_group, cpu) CFS bandwidth state, moved out of struct cfs_rq\n"
                     f" * so struct rq keeps its stock CRC surface. */\n"
                     f"struct cfs_bw_cpu {{\n{body}}};\n")
        anchor = "struct cfs_bandwidth {"
        if anchor not in src:
            die("sched.h: struct cfs_bandwidth not found")
        src = src.replace(anchor, newstruct + "\n" + anchor, 1)

        # ---- 3. hang it off struct cfs_bandwidth ----------------------------
        m2 = re.search(r"(struct cfs_bandwidth \{\n)(#ifdef CONFIG_CFS_BANDWIDTH\n)", src)
        if not m2:
            die("sched.h: struct cfs_bandwidth has no CONFIG_CFS_BANDWIDTH block")
        ins = m2.end()
        src = (src[:ins] +
               "\t/* per-cpu bandwidth state for the cfs_rq's of this group */\n"
               "\tstruct cfs_bw_cpu\t*cpu_state;\n" +
               src[ins:])

        # ---- 4. accessor at the end of sched.h ------------------------------
        # It needs struct rq complete, which is defined well after struct
        # task_group, so it goes in just before the header's closing #endif.
        guard = re.search(r"\n#endif /\* _KERNEL_SCHED_SCHED_H \*/\s*$", src)
        if not guard:
            die("sched.h: could not find the closing header guard")
        src = src[:guard.start()] + "\n" + FALLBACK + src[guard.start():]

        # per_cpu() needs the percpu header explicitly
        if "#include <linux/percpu.h>" not in src:
            src = src.replace("#include <linux/sched/affinity.h>",
                              "#include <linux/percpu.h>\n#include <linux/sched/affinity.h>", 1)

        write(sched_h, src)
        print("  sched.h: block moved to struct cfs_bw_cpu, cpu_state added, accessor added")

        # ---- 5. rewrite every access site -----------------------------------
        pat = re.compile(r"\b(\w+)->(" + "|".join(re.escape(f) for f in sorted(fields, key=len, reverse=True)) + r")\b")
        total = 0
        for rel in ("kernel/sched/fair.c", "kernel/sched/core.c", "kernel/sched/debug.c",
                    "kernel/sched/stats.c", "kernel/sched/pelt.h", "kernel/sched/pelt.c"):
            p = os.path.join(root, rel)
            if not os.path.exists(p):
                continue
            s = read(p)
            # rewrite code only - comment lines mention these fields too, and
            # rewriting prose produces nonsense like "CFS_BW(cfs)@cfs_rq_idle"
            out = []
            n = 0
            for line in s.split("\n"):
                st = line.lstrip()
                if st.startswith(("*", "/*", "//")):
                    out.append(line)
                    continue
                new_line, k = pat.subn(lambda mm: f"CFS_BW({mm.group(1)})->{mm.group(2)}", line)
                out.append(new_line)
                n += k
            new = "\n".join(out)
            if n:
                write(p, new)
                print(f"  {rel}: {n} access site(s) rewritten")
                total += n
        if not total:
            die("no access sites rewritten - the field list did not match this tree")

        # ---- 5b. the throttled-list walk ------------------------------------
        # list_for_each_entry_rcu(cfs_rq, &cfs_b->throttled_cfs_rq, throttled_list)
        # passes the member name as an argument, not through "->", so it is not an
        # access site - it has to be converted to iterate the new block.
        s = read(fair_c)
        walk = re.search(
            r"\tlist_for_each_entry_rcu\(cfs_rq, &cfs_b->throttled_cfs_rq,\n"
            r"\t+throttled_list\) \{\n"
            r"\t\tstruct rq \*rq = rq_of\(cfs_rq\);\n"
            r"\t\tstruct rq_flags rf;\n", s)
        if walk:
            # assign into the existing cfs_rq rather than shadowing it - an
            # unused outer variable is a -Werror build failure
            s = s[:walk.start()] + (
                "\tlist_for_each_entry_rcu(bw, &cfs_b->throttled_cfs_rq,\n"
                "\t\t\t\tthrottled_list) {\n"
                "\t\tstruct rq *rq;\n"
                "\t\tstruct rq_flags rf;\n"
                "\n"
                "\t\tcfs_rq = cfs_bw_to_cfs_rq(cfs_b, bw);\n"
                "\t\trq = rq_of(cfs_rq);\n"
            ) + s[walk.end():]
            decl = re.search(r"(static void distribute_cfs_runtime\(struct cfs_bandwidth \*cfs_b\)\n\{\n)(\tstruct cfs_rq \*cfs_rq;\n)", s)
            if not decl:
                die("fair.c: distribute_cfs_runtime() declaration block not found")
            s = s[:decl.end()] + "\tstruct cfs_bw_cpu *bw;\n" + s[decl.end():]
            write(fair_c, s)
            print("  fair.c: throttled-list walk converted to cfs_bw_cpu")
        else:
            print("  fair.c: no throttled-list walk found (already converted?)")

        # ---- 6. allocate / free ---------------------------------------------
        s = read(fair_c)

        if "cfs_bw_nostate" not in s:
            # define the fallback block next to the other cfs bandwidth state
            anchor = "void init_cfs_bandwidth(struct cfs_bandwidth *cfs_b)"
            if anchor not in s:
                die("fair.c: init_cfs_bandwidth() not found")
            s = s.replace(anchor,
                          "DEFINE_PER_CPU(struct cfs_bw_cpu, cfs_bw_nostate);\n\n" + anchor, 1)

            # allocate in init_cfs_bandwidth, after the timers are set up
            m4 = re.search(r"(void init_cfs_bandwidth\(struct cfs_bandwidth \*cfs_b\)\n\{\n)(.*?)(\n\}\n)", s, re.S)
            if not m4:
                die("fair.c: could not read init_cfs_bandwidth() body")
            body_a = m4.group(2).rstrip("\n")
            body_a += ("\n\n\t/* zero-drift port: per-cpu state for this group's cfs_rq's */\n"
                       "\tcfs_b->cpu_state = kcalloc(nr_cpu_ids, sizeof(struct cfs_bw_cpu),\n"
                       "\t\t\t\t     GFP_KERNEL);\n"
                       "\tif (!cfs_b->cpu_state)\n"
                       "\t\tWARN_ONCE(1, \"cfs bandwidth: no per-cpu state, limits disabled\\n\");\n")
            s = s[:m4.start()] + m4.group(1) + body_a + m4.group(3) + s[m4.end():]

            # free in destroy_cfs_bandwidth
            m5 = re.search(r"(static void destroy_cfs_bandwidth\(struct cfs_bandwidth \*cfs_b\)\n\{\n)(.*?)(\n\}\n)", s, re.S)
            if not m5:
                die("fair.c: could not read destroy_cfs_bandwidth() body")
            body_d = m5.group(2)
            body_d += "\n\n\tkfree(cfs_b->cpu_state);\n\tcfs_b->cpu_state = NULL;\n"
            s = s[:m5.start()] + m5.group(1) + body_d + m5.group(3) + s[m5.end():]

            write(fair_c, s)
            print("  fair.c: cpu_state allocated in init_cfs_bandwidth(), freed in destroy")

    # ---- verify -------------------------------------------------------------
    problems = []
    sched_now = read(sched_h)
    m = re.search(r"struct cfs_rq \{.*?\n\};", sched_now, re.S)
    if "CONFIG_CFS_BANDWIDTH" in m.group(0):
        problems.append("struct cfs_rq still references CONFIG_CFS_BANDWIDTH")
    for needle, where in ((MARKER, "sched.h"), ("cpu_state", "sched.h"),
                          ("cfs_rq_bw(struct cfs_rq", "sched.h")):
        if needle not in sched_now:
            problems.append(f"{where}: missing {needle!r}")
    s = read(fair_c)
    for needle in ("cfs_bw_nostate", "cfs_b->cpu_state = kcalloc", "kfree(cfs_b->cpu_state)"):
        if needle not in s:
            problems.append(f"fair.c: missing {needle!r}")
    # any remaining direct access means a site was missed -> compile error later;
    # the field list comes from the struct we just wrote, so an idempotent re-run
    # checks the same set
    bw = re.search(r"struct cfs_bw_cpu \{\n(.*?)\n\};", sched_now, re.S)
    fields_used = field_names(bw.group(1)) if bw else []
    leftovers = []
    for rel in ("kernel/sched/fair.c", "kernel/sched/core.c", "kernel/sched/debug.c",
                "kernel/sched/stats.c", "kernel/sched/pelt.h", "kernel/sched/pelt.c"):
        fp = os.path.join(root, rel)
        if not os.path.exists(fp):
            continue
        for i, line in enumerate(read(fp).splitlines(), 1):
            st = line.lstrip()
            if st.startswith(("*", "/*", "//")):
                continue
            for f in fields_used:
                if re.search(r"->" + re.escape(f) + r"\b", line) and "CFS_BW(" not in line:
                    leftovers.append(f"{rel}:{i}: {st[:80]}")
    for rel in ("kernel/sched/fair.c", "kernel/sched/core.c", "kernel/sched/debug.c"):
        fp = os.path.join(root, rel)
        if not os.path.exists(fp):
            continue
        for i, line in enumerate(read(fp).splitlines(), 1):
            if re.search(r"[, ]throttled_list\)", line) and "list_for_each" not in line:
                leftovers.append(f"{rel}:{i}: bare throttled_list argument")

    if leftovers:
        problems.append("unrewritten access: " + "; ".join(leftovers[:5]))

    if problems:
        die("verification failed:\n  - " + "\n  - ".join(problems))
    print("  verified: cfs_rq stock, state on cfs_bandwidth, alloc/free wired")
    print("[+] done")


if __name__ == "__main__":
    main()
