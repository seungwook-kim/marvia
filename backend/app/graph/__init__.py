"""판정 그래프 패키지 (docs/specs/v2-langgraph.md 2절). langgraph import는 이 패키지 안에서만 한다.

state.py(State), checkpoint.py(체크포인트 암호화 직렬화기), nodes.py(노드), routes.py(갈림길), build.py(조립), facade.py(진입점).
routers/agent.py는 AGENT_GRAPH=on일 때만 facade를 함수 안에서 불러 온다. 이 파일은 아무것도 import하지 않는다(순환과 langgraph 적재 방지).
"""
