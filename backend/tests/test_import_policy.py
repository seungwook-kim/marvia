"""설치·import 정책 검사 (docs/specs/v2-langgraph.md 1절, AC-15, 작업 H0).

레포가 지키기로 한 import 경계를 코드로 막는다. 문서(CLAUDE.md, SDD 1)에 적은 선이 어느 날 조용히 깨지는 것을 막으려는 것이다.
- langchain, langchain-anthropic 같은 상위 패키지는 설치하지 않는다. langchain-core와 langsmith는 langgraph가 필수로 끌고 오는
  의존성이라 설치돼 있지만 레포 코드는 import하지 않는다.
- langgraph import는 backend/app/graph/와 worker/ 안에서만 한다 (테스트는 test_graph_*.py만).
- LLM SDK(openai, anthropic) import는 backend/app/services/llm.py 하나뿐이다 (CLAUDE.md "하지 말 것").
- LangSmith 트레이싱을 켜는 환경변수는 .env.example에 두지 않는다.

검사는 소스 파일을 읽어 import 문만 본다(ast). 모듈을 실제로 import하지 않으므로 부작용이 없고, 주석·문자열에 적힌 이름은 걸리지 않는다.
키도 DB도 없이 돈다.
"""
import ast
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# 레포가 직접 만든 파이썬 코드가 있는 곳. .venv, node_modules, 작업 사본(.claude, 임시폴더)은 레포 코드가 아니라 뺀다
SOURCE_ROOTS = ["backend", "mcp_server", "worker", "relay", "evals", "scripts"]
SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".git"}

# langgraph를 import해도 되는 곳 (v2-langgraph.md 1절). 상대 경로는 REPO 기준 posix 문자열
LANGGRAPH_ALLOWED_PREFIXES = ("backend/app/graph/", "worker/")
# 그래프 테스트는 langgraph를 직접 불러 검사한다 (H1~H11의 test_graph_*.py)
LANGGRAPH_ALLOWED_TEST = "backend/tests/test_graph_"

# LLM SDK를 import해도 되는 유일한 파일 (CLAUDE.md "하지 말 것", SDD 5.5)
LLM_SDK_MODULES = {"openai", "anthropic"}
LLM_SDK_ALLOWED = "backend/app/services/llm.py"

# 레포 코드가 import하면 안 되는 상위 패키지와 끌려온 의존성. langgraph 설치 때문에 langchain_core, langsmith는 설치돼 있어도
# 코드는 쓰지 않는다 (v2-langgraph.md 1절 "주의 하나", Q1)
FORBIDDEN_EVERYWHERE = {"langchain", "langchain_anthropic", "langchain_core", "langsmith"}


def _python_files():
    """SOURCE_ROOTS 아래의 모든 .py 파일과 레포 루트의 .py 파일을 돌려준다."""
    files = list(REPO.glob("*.py"))
    for root in SOURCE_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if SKIP_DIRS.intersection(path.parts):
                continue
            files.append(path)
    return files


def _imported_top_levels(path: Path):
    """파일이 import하는 최상위 패키지 이름 집합. 읽을 수 없거나 문법 오류인 파일은 빈 집합(다른 테스트가 잡는다)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0은 상대 import라 레포 안 모듈이다
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _importers(top_level_names):
    """top_level_names 중 하나라도 import하는 레포 파일의 상대 경로 목록."""
    found = []
    for path in _python_files():
        if _imported_top_levels(path) & set(top_level_names):
            found.append(_rel(path))
    return sorted(found)


def test_scan_finds_the_known_llm_importer():
    """스캔이 실제로 파일을 읽고 있는지 확인한다. llm.py가 openai를 import하므로 반드시 잡혀야 한다.
    경로가 틀려 아무것도 스캔하지 못하면 아래 금지 검사들이 조용히 통과해 버리기 때문이다."""
    assert LLM_SDK_ALLOWED in _importers(LLM_SDK_MODULES)


def test_langchain_parent_packages_are_not_installed():
    """langchain, langchain-anthropic 본체는 설치돼 있지 않다 (v2-langgraph.md 1절, 의존성 해석 결과).
    find_spec은 모듈을 실행하지 않고 설치 여부만 본다."""
    assert importlib.util.find_spec("langchain") is None
    assert importlib.util.find_spec("langchain_anthropic") is None


def test_repo_code_never_imports_langchain_family():
    """langchain, langchain_anthropic은 물론 langgraph가 끌고 온 langchain_core, langsmith도 레포 코드에서 import하지 않는다."""
    assert _importers(FORBIDDEN_EVERYWHERE) == []


def test_langgraph_import_only_in_graph_and_worker():
    """langgraph(체크포인터 langgraph.checkpoint.postgres 포함)를 import하는 파일은 backend/app/graph/, worker/,
    그래프 테스트(test_graph_*.py)뿐이다. 라우터, 서비스, MCP 도구가 그래프 라이브러리에 직접 묶이지 않게 한다."""
    offenders = [
        rel
        for rel in _importers({"langgraph"})
        if not rel.startswith(LANGGRAPH_ALLOWED_PREFIXES) and not rel.startswith(LANGGRAPH_ALLOWED_TEST)
    ]
    assert offenders == []


def test_llm_sdk_import_only_in_llm_py():
    """openai, anthropic SDK는 backend/app/services/llm.py에서만 import한다. 라우터, MCP 도구, 워커, 그래프 노드, 평가 코드는
    LLM을 직접 부르지 않고 llm.py의 함수를 부른다."""
    offenders = [rel for rel in _importers(LLM_SDK_MODULES) if rel != LLM_SDK_ALLOWED]
    assert offenders == []


def test_env_example_does_not_enable_langsmith_tracing():
    """.env.example에 LangSmith 트레이싱을 켜는 변수 이름이 없다. langsmith는 이 변수가 있어야만 동작하므로,
    배포가 예제 파일을 그대로 베껴도 꺼진 채로 남는다 (AC-15)."""
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        assert name not in text, f".env.example에 {name}이 있다"
