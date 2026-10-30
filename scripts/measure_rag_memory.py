"""Render 무료 배포 이미지의 메모리 실측 (docs/specs/v2-rag.md 작업 R14, SDD 6.8 "메모리" 줄과 같은 방식).

deploy/render/Dockerfile로 만든 이미지를 --memory 512m --cpus 0.1(Render 무료 인스턴스와 같은 한도)로 REPLAY_MODE=true, 키 없이 띄우고
단계마다 메모리를 잰다. 결과는 evals/results/rag_memory_<시각>.json이다.
  (a) 대기: 서버가 /health에 답한 뒤 가만히 둔 상태
  (b) 데모 조회 1건 뒤 (POST /api/agent, 재생 모드), 그리고 Kiwi(V14 명사 추출)가 서버에 올라왔는지 /proc/<pid>/maps로 확인한다
  (c) 서류 사진 OCR 1장 뒤 (POST /api/document, 레포의 가공 fixture 이미지)
  (d) 채팅 RAG: RAG_MODE=bm25 Searcher를 만들고 질의를 돌린 뒤. 재생 모드는 RAG를 끄므로(services/rag/runtime.rag_mode) 서버 안에서는 잴 수 없어,
      같은 한도의 새 컨테이너에서 별도 프로세스(--inside)가 단계마다 RSS를 적는다: 백엔드 import, Kiwi 만들기, Kiwi 첫 분석, Searcher 만들기, 질의.
      Kiwi 설정 두 가지(default, no_multi=Kiwi(load_multi_dict=False))를 각각 512m 한도와 4g(한도 없음에 가까움) 두 번씩 돈다.
      마지막으로 서버 컨테이너 안에서 같은 프로세스(no_multi)를 같이 돌려 한도 안에 같이 들어가는지 본다(서버가 OOM으로 죽으면 결과에 적힌다).
hybrid는 Render 이미지에 임베딩 모델이 없어 잴 수 없다(이미지는 모델과 requirements-rag.txt를 넣지 않는다, docs/specs/v2-rag.md Q5).
측정 단위는 MB(10^6이 아니라 2^20 바이트). 컨테이너 총량은 cgroup memory.current(페이지 캐시 포함)이고 프로세스 값은 VmRSS다.
실행: python -m scripts.measure_rag_memory --image <이미지>   (Docker가 필요하다. 만든 컨테이너는 끝에 지운다. 이미지는 지우지 않는다)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
KIB = 1024
HEALTH_WAIT_SEC = 240
SETTLE_SEC = 15
DEMO_BL = "DEMOQD2610001"  # scripts/make_demo.py가 만드는 재생용 모의 B/L (실제 번호가 아니다)
DOC_FIXTURE = REPO_ROOT / "backend" / "tests" / "fixtures" / "documents" / "notice_phone.jpg"
# SDD 6.8 "메모리" 줄의 기존 실측 (512MB 제한 로컬 실행, 이 스크립트 이전)
SDD_6_8 = {"idle_mb": 154, "after_demo_lookup_mb": 178, "after_ocr_1_mb": 201, "source": "docs/Marvia_SDD.md 6.8 메모리 줄"}
SAMPLE_SH = (
    "cat /sys/fs/cgroup/memory.current 2>/dev/null || cat /sys/fs/cgroup/memory/memory.usage_in_bytes; echo ---; "
    "grep -E '^(anon|file|kernel|shmem) ' /sys/fs/cgroup/memory.stat 2>/dev/null; echo ---; "
    "for p in /proc/[0-9]*; do r=$(awk '/^VmRSS/{print $2}' $p/status 2>/dev/null); "
    "[ -n \"$r\" ] && echo \"${p#/proc/} $r $(tr '\\0' ' ' < $p/cmdline | cut -c1-70)\"; done; echo ---; "
    "for p in /proc/[0-9]*; do grep -q kiwipiepy $p/maps 2>/dev/null && echo kiwi ${p#/proc/}; done"
)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def docker_exec(name: str, script: str, env: dict | None = None, timeout: int = 600) -> str:
    args = ["docker", "exec"]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    result = run([*args, name, "sh", "-c", script], timeout=timeout)
    return result.stdout


def sample(name: str, label: str) -> dict:
    """컨테이너 안에서 총량(cgroup)과 프로세스별 RSS, Kiwi가 올라온 프로세스를 읽는다."""
    parts = docker_exec(name, SAMPLE_SH).split("---\n")
    total = int(parts[0].strip().splitlines()[0])
    stat = {line.split()[0]: int(line.split()[1]) for line in parts[1].strip().splitlines() if line.strip()}
    procs = []
    for line in parts[2].strip().splitlines():
        pid, rss_kb, cmd = (line.split(None, 2) + [""])[:3]
        procs.append({"pid": int(pid), "rss_mb": round(int(rss_kb) * KIB / MIB, 1), "cmd": cmd.strip()})
    kiwi = [int(line.split()[1]) for line in parts[3].strip().splitlines() if line.startswith("kiwi")]
    server = next((p for p in procs if "uvicorn" in p["cmd"]), None)
    worker = next((p for p in procs if "worker.run" in p["cmd"]), None)
    return {
        "label": label, "container_mb": round(total / MIB, 1),
        "anon_mb": round(stat.get("anon", 0) / MIB, 1), "file_cache_mb": round(stat.get("file", 0) / MIB, 1),
        "uvicorn_rss_mb": server["rss_mb"] if server else None, "worker_rss_mb": worker["rss_mb"] if worker else None,
        "kiwi_loaded_in_server": bool(server and server["pid"] in kiwi),
        "processes": [p for p in procs if p["rss_mb"] >= 10],
    }


def http(port: int, path: str, body: bytes | None = None, headers: dict | None = None, timeout: int = 300) -> tuple[int, bytes]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers or {}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def multipart(path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()
    return head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def measure_inside(args: argparse.Namespace, kiwi: str = "default", memory: str | None = None) -> tuple[dict | None, list[str]]:
    """RAG 측정 프로세스만 도는 새 컨테이너를 띄워 --inside 결과(JSON 한 줄)를 읽는다. 한도는 서버와 같고 memory로 바꿀 수 있다.
    kiwi: default(validate._kiwi 그대로) | no_multi(Kiwi(load_multi_dict=False), 메모리를 줄이는 후보 설정).
    스크립트는 이미지에 들어 있는 사본이 아니라 지금 파일을 읽기 전용으로 마운트해 쓴다."""
    name = f"marvia-r14-rag-{uuid.uuid4().hex[:6]}"
    here = Path(__file__).resolve().parent
    memory = memory or args.memory
    cmd = ["docker", "run", "--name", name, "--memory", memory, "--memory-swap", memory]
    if args.cpus:
        cmd += ["--cpus", args.cpus]
    cmd += ["-e", "REPLAY_MODE=false", "-e", "RAG_MODE=bm25", "-e", "RAG_STORE=file", "-e", f"R14_KIWI={kiwi}",
            "-v", f"{here / 'measure_rag_memory.py'}:/app/scripts/measure_rag_memory.py:ro",
            "-v", f"{here / 'measure_rag_latency.py'}:/app/scripts/measure_rag_latency.py:ro",
            args.image, "sh", "-c", "cd /app && python -m scripts.measure_rag_memory --inside 2>&1"]
    notes: list[str] = []
    try:
        out = run(cmd, timeout=1200).stdout
        state = json.loads(run(["docker", "inspect", name, "--format", "{{json .State}}"]).stdout)
        notes.append(f"RAG 측정 컨테이너(kiwi={kiwi}, memory={memory}): OOMKilled={state.get('OOMKilled')}, ExitCode={state.get('ExitCode')}")
        marks = [json.loads(line[5:]) for line in out.splitlines() if line.startswith("MARK ")]
        try:
            return json.loads(out.strip().splitlines()[-1]), notes
        except (ValueError, IndexError):
            notes.append(f"--inside가 끝까지 가지 못했다. 마지막 출력: {out[-200:]!r}")
            return ({"incomplete": True, "marks": marks} if marks else None), notes
    finally:
        run(["docker", "rm", "-f", name])


def orchestrate(args: argparse.Namespace) -> int:
    name = f"marvia-r14-{uuid.uuid4().hex[:6]}"
    env = {"REPLAY_MODE": "true", "PORT": "10000", "KEEP_AWAKE": "off", "DOC_WARMUP": "off", "WORKER_SCHEDULE": "off", "WATCH_ENABLED": "off"}
    cmd = ["docker", "run", "-d", "--name", name, "--memory", args.memory, "--memory-swap", args.memory, "-p", f"127.0.0.1:{args.port}:10000"]
    if args.cpus:
        cmd += ["--cpus", args.cpus]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    started = run([*cmd, args.image])
    if started.returncode != 0:
        print(started.stderr, file=sys.stderr)
        return 1
    steps: list[dict] = []
    inside_runs: list[dict] = []
    notes: list[str] = []
    try:
        began = time.monotonic()
        while time.monotonic() - began < HEALTH_WAIT_SEC:
            try:
                if http(args.port, "/health", timeout=5)[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(2)
        else:
            print(run(["docker", "logs", "--tail", "40", name]).stdout)
            raise SystemExit("서버가 /health에 답하지 않았다")
        ready_sec = round(time.monotonic() - began, 1)
        time.sleep(SETTLE_SEC)
        steps.append(sample(name, "a_idle"))

        body = json.dumps({"bl_no": DEMO_BL, "session_id": "r14"}).encode()
        status, _ = http(args.port, "/api/agent", body, {"Content-Type": "application/json"})
        notes.append(f"POST /api/agent {DEMO_BL} -> HTTP {status}")
        steps.append(sample(name, "b_after_demo_lookup"))

        if not steps[-1]["kiwi_loaded_in_server"]:
            chat = json.dumps({"message": "A/N 도착통지서 못 받으면 어떻게 해?", "session_id": "r14"}).encode()
            status, _ = http(args.port, "/api/chat", chat, {"Content-Type": "application/json"})
            notes.append(f"POST /api/chat (실무 질문) -> HTTP {status}")
            steps.append(sample(name, "b2_after_chat_question"))
        if not any(s["kiwi_loaded_in_server"] for s in steps):
            notes.append("재생 모드에서는 서버에 Kiwi(V14)가 올라오지 않았다: Kiwi 비용은 --inside 측정의 s1 증가분으로 따로 본다")

        payload, content_type = multipart(DOC_FIXTURE)
        status, _ = http(args.port, "/api/document", payload, {"Content-Type": content_type})
        notes.append(f"POST /api/document ({DOC_FIXTURE.name}) -> HTTP {status}")
        steps.append(sample(name, "c_after_ocr_1"))

        # 서버 컨테이너 안에서 RAG 측정 프로세스를 같이 돌려 본다(Kiwi 후보 설정): 한도 안에 같이 들어가는가. 넘으면 서버가 OOM으로 죽는다
        run(["docker", "cp", str(Path(__file__).resolve()), f"{name}:/app/scripts/measure_rag_memory.py"])
        run(["docker", "cp", str(Path(__file__).resolve().parent / "measure_rag_latency.py"), f"{name}:/app/scripts/measure_rag_latency.py"])
        joint = docker_exec(name, "cd /app && python -m scripts.measure_rag_memory --inside 2>&1",
                            env={"REPLAY_MODE": "false", "RAG_MODE": "bm25", "RAG_STORE": "file", "R14_KIWI": "no_multi"})
        joint_marks = [json.loads(line[5:]) for line in joint.splitlines() if line.startswith("MARK ")]
        state = json.loads(run(["docker", "inspect", name, "--format", "{{json .State}}"]).stdout)
        oom = bool(state.get("OOMKilled"))
        joint_result = {"label": "joint_in_server_container_no_multi_kiwi", "server_survived": bool(state.get("Running")), "oom_killed": oom,
                        "inside_marks": joint_marks, "inside_finished": bool(joint.strip().splitlines() and joint.strip().splitlines()[-1].startswith("{"))}
        if state.get("Running"):
            steps.append(sample(name, "d_after_joint_process_exit"))
        limits = json.loads(run(["docker", "inspect", name, "--format", "{{json .HostConfig}}"]).stdout)
        applied = {"memory_bytes": limits.get("Memory"), "nano_cpus": limits.get("NanoCpus")}
        # (d) 채팅 RAG는 서버와 따로, 같은 한도의 새 컨테이너에서 재운다. 서버 컨테이너에 같이 넣으면 서버(OCR 뒤 큰 쪽)가 먼저 OOM으로 죽어 증가분을 못 잰다
        run(["docker", "rm", "-f", name])
        for label, kiwi, memory in (("limit_default_kiwi", "default", None), ("nolimit_default_kiwi", "default", "4g"),
                                    ("limit_no_multi_kiwi", "no_multi", None), ("nolimit_no_multi_kiwi", "no_multi", "4g")):
            result, inside_notes = measure_inside(args, kiwi, memory)
            inside_runs.append({"label": label, "kiwi": kiwi, "memory": memory or args.memory, "result": result})
            notes.extend(inside_notes)
    finally:
        run(["docker", "rm", "-f", name])
    report = {
        "what": "deploy/render/Dockerfile 이미지의 메모리 단계별 실측 (REPLAY_MODE=true, 키 없음)",
        "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "image": args.image, "limits_requested": {"memory": args.memory, "cpus": args.cpus}, "limits_applied": applied,
        "host": {"platform": platform.platform(), "docker_server": run(["docker", "version", "--format", "{{.Server.Version}}"]).stdout.strip()},
        "ready_after_sec": ready_sec, "oom_killed": oom, "steps": steps, "rag_inside_process_runs": inside_runs, "joint_run": joint_result, "notes": notes,
        "sdd_6_8_reference": SDD_6_8,
        "caveat": "Docker Desktop(WSL2) 위의 값이다. Render의 Linux 호스트와 다를 수 있다. hybrid는 이미지에 모델이 없어 잴 수 없다.",
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"rag_memory_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(path)
    for s in steps:
        print(f"{s['label']}: container={s['container_mb']}MB uvicorn_rss={s['uvicorn_rss_mb']}MB kiwi_in_server={s['kiwi_loaded_in_server']}")
    print("oom_killed:", oom)
    for run_ in inside_runs:
        print("inside", run_["label"], json.dumps(run_["result"], ensure_ascii=False)[:1500])
    return 0


# ── 컨테이너 안의 별도 프로세스 ──

def _status_kb(field: str) -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(field):
            return int(re.findall(r"\d+", line)[0])
    return 0


def _cgroup_bytes() -> int:
    for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            return int(Path(path).read_text())
        except OSError:
            continue
    return 0


def inside() -> int:
    """RAG_MODE=bm25 Searcher의 메모리 증가분과 검색 지연. 단계마다 이 프로세스의 RSS와 컨테이너 총량을 적는다."""
    sys.path.insert(0, str(REPO_ROOT if (REPO_ROOT / "backend").exists() else Path("/app")))
    marks: list[dict] = []

    def mark(label: str) -> None:
        marks.append({"label": label, "process_rss_mb": round(_status_kb("VmRSS") * KIB / MIB, 1),
                      "process_peak_mb": round(_status_kb("VmHWM") * KIB / MIB, 1), "container_mb": round(_cgroup_bytes() / MIB, 1)})
        print("MARK " + json.dumps(marks[-1], ensure_ascii=False), flush=True)  # 중간에 OOM으로 죽어도 어디까지 갔는지 남는다

    mark("s0_python_start")
    from backend.app.services import validate
    from backend.app.services.rag import runtime
    from scripts.measure_rag_latency import heading_queries, summarize
    mark("s0b_backend_imported")
    t = time.perf_counter()
    if os.environ.get("R14_KIWI") == "no_multi":
        from functools import lru_cache

        from kiwipiepy import Kiwi

        validate._kiwi = lru_cache(maxsize=1)(lambda: Kiwi(load_multi_dict=False))  # 후보 설정: 다어절 사전을 읽지 않는다
    kiwi = validate._kiwi()  # V14 명사 추출이 처음 부를 때 올라오는 Kiwi (서버에서는 이미 올라온 상태가 기준선이다)
    kiwi_sec = round(time.perf_counter() - t, 2)
    mark("s1_kiwi_created")
    kiwi.tokenize("도착통지서")  # 첫 분석 때 늘어나는 메모리까지 센다
    mark("s1b_kiwi_first_tokenize")
    t = time.perf_counter()
    searcher = runtime.active_searcher()
    build_sec = round(time.perf_counter() - t, 2)
    mark("s2_bm25_searcher_built")
    if searcher is None:
        print(json.dumps({"error": "Searcher를 만들 수 없다", "marks": marks}, ensure_ascii=False))
        return 1
    queries = heading_queries()
    latency: dict = {}
    for kind, texts in queries.items():
        for text in texts:
            runtime.run_search(searcher, [text])  # 예열
        samples = []
        for _ in range(5):
            for text in texts:
                t = time.perf_counter()
                runtime.run_search(searcher, [text])
                samples.append((time.perf_counter() - t) * 1000)
        latency[kind] = summarize(samples)
    mark("s3_after_queries")
    hybrid = {"onnxruntime_installed": bool(importlib.util.find_spec("onnxruntime")),
              "model_dir_present": (Path("/app/models/rag").exists() and any(Path("/app/models/rag").iterdir()))}
    print(json.dumps({"marks": marks, "kiwi_load_sec": kiwi_sec, "searcher_build_sec": build_sec, "chunks": len(searcher._chunks),
                      "bm25_tool_latency": latency, "hybrid_possible_in_image": hybrid}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Render 무료 이미지 메모리 실측 (R14)")
    parser.add_argument("--image", default="marvia-r14-mem:tmp")
    parser.add_argument("--memory", default="512m")
    parser.add_argument("--cpus", default="0.1", help="빈 문자열이면 CPU 제한을 걸지 않는다")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "evals" / "results"))
    parser.add_argument("--inside", action="store_true", help="컨테이너 안에서만 쓰는 모드")
    args = parser.parse_args()
    return inside() if args.inside else orchestrate(args)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
