"""One-off driver: take an already-running MiroFish simulation through to its
report + extracted crowd probability (used when the original driver process ended
mid-pipeline). Writes mirofish_result.json. Run:
    python -m harness.mirofish_resume <simulation_id>
"""
import sys, time, json
import httpx
from harness import mirofish as mf


def resume(sid: str, base: str = "http://localhost:5001", max_wait: int = 2400):
    client = httpx.Client(base_url=base, timeout=30)
    # recover the market question from the sim's project
    question = "(market)"
    try:
        sim = mf._get(client, f"/api/simulation/{sid}")
        pid = sim.get("project_id")
        if pid:
            proj = mf._get(client, f"/api/graph/project/{pid}")
            question = proj.get("simulation_requirement") or proj.get("name") or question
    except Exception as e:
        print("[resume] question lookup warn:", e)
    print(f"[resume] sid={sid} question={question[:90]!r}", flush=True)

    deadline = time.monotonic() + max_wait
    # 1) wait for the OASIS simulation run to finish
    try:
        run = mf._poll_run(client, sid, deadline)
        print(f"[resume] sim run finished: {str(run)[:140]}", flush=True)
    except Exception as e:
        print("[resume] poll_run warn:", e, flush=True)

    # 2) trigger report generation + poll
    md, rid = "", None
    try:
        gen = mf._post_json(client, "/api/report/generate", {"simulation_id": sid})
        task_id, rid = gen.get("task_id"), gen.get("report_id")
        print(f"[resume] report task={task_id} report_id={rid}", flush=True)
        rep = mf._poll_report(client, task_id, sid, deadline)
        rid = rid or rep.get("report_id")
        print(f"[resume] report status: {str(rep)[:140]}", flush=True)
        if rid:
            md = mf._get(client, f"/api/report/{rid}").get("markdown_content", "")
    except Exception as e:
        print("[resume] report warn:", e, flush=True)

    # 3) extract a YES probability from the report
    prob = None
    if md:
        try:
            prob = mf._extract_probability(md, question)
        except Exception as e:
            print("[resume] extract warn:", e, flush=True)

    out = {"sid": sid, "question": question, "probability": prob,
           "report_chars": len(md), "report_excerpt": md[:2500]}
    with open("mirofish_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[resume] DONE  probability={prob}  report_chars={len(md)}  -> mirofish_result.json", flush=True)


if __name__ == "__main__":
    resume(sys.argv[1] if len(sys.argv) > 1 else "sim_e81a4c476f1c")
