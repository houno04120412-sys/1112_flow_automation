# 인계 프롬프트

다른 컴퓨터에서 이 저장소를 클론한 뒤, 아래 블록을 에이전트(Claude)에게 그대로 붙여 넣는다.

```
이 저장소(1112_flow_automation)는 Google Flow(https://flow.google.com/)의 무료 모델 Nano Banana 2.1로
이미지를 대량 생성하는 에이전트 워크플로우다. 이 작업은 Claude만 사용한다.

먼저 읽을 것: record.md(현재 상태·다음 행동), docs/PRD.md(설계·실측값·차단 회피 규칙), docs/WORKFLOW.md(실행 절차).

환경 준비:
1. Python 3 + Pillow, Node, Aside CLI(aside)가 필요하다. `python tools/flow_run.py --self-test`와
   `python tools/review.py --self-test`가 둘 다 PASS인지 확인한다.
2. Aside 브라우저에서 flow.google.com에 로그인돼 있는지 확인한다(안 돼 있으면 사용자에게 로그인을 요청).
3. 작업용 Flow 프로젝트 URL을 사용자에게 받거나 Flow에서 새 프로젝트를 만들어 FLOW_PROJECT_URL로 쓴다.
   (기존 컴퓨터의 시험 프로젝트 URL은 같은 계정이면 그대로 열린다:
   https://flow.google.com/project/e274e3f2-100b-4579-a090-f3c7198d97a9)
4. 소량 시험: 프롬프트 2개(x2 1건, ref_title 참조 수정 1건)로 flow_run → review check → review montage를
   돌려 몽타주가 프롬프트와 맞는지 확인한다. 화면 문구가 바뀌어 실패하면 tools/flow_run.py의 JS_TEMPLATE만 고친다.

규칙: 계정 1개, 실제 로그인 브라우저에서만 실행, Flow 내부 API 직접 호출 금지, 기본 간격·휴식·max-items를 낮추지 않는다.
오류·한도·캡차 문구로 종료코드 2가 나면 멈추고 사용자에게 알린다. 원본은 덮어쓰지 않고 수정본은 _2, _3 번호를 쓴다.

다음 작업은 record.md의 "다음 행동"을 따른다.
```
