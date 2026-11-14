# @module {"id":"vm.cli","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors","vm.store"],"book":"HB-VC-s7-vmem"}
"""@module vm.cli - `vmem <verb> --store S ...`: exactly one canonical JSON object on stdout, rc per SPEC_INT R4."""
import argparse
import os
import sys

from visioncore import canon
from visioncore.errors import VqaError, result_obj, result_line
from . import gates, router
from .store import Store

VERBS = ("route", "answer", "get", "evidence", "last-good", "diff-region", "seen-defect", "check-answer", "verify",
         "rollback", "ingest", "compact", "gc", "pin")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise VqaError("E_USAGE", {"reason": "argparse", "msg": message[:120]})

    def exit(self, status=0, message=None):
        if status:
            raise VqaError("E_USAGE", {"reason": "argparse_exit"})
        raise SystemExit(0)


def _parser():
    p = _Parser(prog="vmem")
    p.add_argument("verb", choices=VERBS)
    p.add_argument("id", nargs="?")
    p.add_argument("--store")
    p.add_argument("--q")
    p.add_argument("--scope")
    p.add_argument("--sig")
    p.add_argument("--tk")
    p.add_argument("--subject")
    p.add_argument("--class", dest="cls")
    p.add_argument("--grid")
    p.add_argument("--build", type=int)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--scan", action="store_true")
    p.add_argument("--from-build", type=int, dest="from_build")
    p.add_argument("--to-build", type=int, dest="to_build")
    p.add_argument("--max-tok", type=int, dest="max_tok", default=router.ANSWER_SOFT_TOK)
    p.add_argument("--answer")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--gen", type=int)
    p.add_argument("--to", type=int)
    p.add_argument("--receipt")
    p.add_argument("--spool")
    return p


def _need(v, name):
    if v is None:
        raise VqaError("E_USAGE", {"reason": "missing_arg", "arg": name})
    return v


def _run(a):
    root = a.store or os.environ.get("VMEM_ROOT")
    root = _need(root, "--store")
    sig = router.parse_sig(a.sig) if a.sig else None
    v = a.verb
    if v == "route":
        return router.route(root, q=a.q, scope=a.scope, sig=sig, tk=a.tk, k=a.k, scan=a.scan, grid=a.grid, build=a.build)
    if v == "answer":
        return router.answer(root, q=a.q, scope=a.scope, sig=sig, tk=a.tk, max_tok=a.max_tok, grid=a.grid)
    if v == "get":
        return router.get(root, _need(a.id, "ID"))
    if v == "evidence":
        return router.evidence(root, _need(a.id, "ID"))
    if v == "last-good":
        return router.last_good(root, tk=a.tk, subject=a.subject, scope=a.scope)
    if v == "diff-region":
        return router.diff_region(root, _need(a.tk, "--tk"), a.from_build, a.to_build)
    if v == "seen-defect":
        return router.seen_defect(root, _need(sig, "--sig"), cls=a.cls, scope=a.scope)
    if v == "check-answer":
        try:
            with open(_need(a.answer, "--answer"), "r", encoding="utf-8") as f:
                ans = canon.loads(f.read())
        except OSError:
            raise VqaError("E_IO", {"reason": "answer_unreadable"})
        return router.check_answer(root, ans)
    if v == "verify":
        res = gates.verify(root, quick=a.quick, gen=a.gen)
        bad = gates.failed(res)
        if bad:
            raise VqaError("E_GATE", {"reason": bad[0], "failed": bad, "gates": res["gates"]})
        return res
    if v == "rollback":
        st = Store(root)
        with st.lock():
            return {"gen": st.rollback(_need(a.to, "--to"))}
    from . import ingest  # ingest / compact / gc / pin persist through one writer module
    return ingest.run_verb(v, root, a)


def main(argv=None):  # @anchor:vm.cli.main
    try:
        a = _parser().parse_args(sys.argv[1:] if argv is None else argv)
        out = result_obj("OK", 0, _run(a))
    except VqaError as e:
        out = e.to_obj()
    except SystemExit:
        return 0
    sys.stdout.write(result_line(out))
    return out["rc"]


if __name__ == "__main__":
    sys.exit(main())
