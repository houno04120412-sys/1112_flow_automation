# 에이전트 절차 — Flow 나노바나나 대량 생성

에이전트는 이 순서만 따른다. 반복 작업은 스크립트가 하고, 토큰은 1단계(기획)와 5단계(몽타주 판정)에만 쓴다.

## 0. 준비 (세션당 1회)
- 브라우저는 Aside가 아니라 **Flow 전용 Windows Chrome**(자동화 전용 프로필)이다. WSL의 Playwright가 DevTools 포트 `127.0.0.1:9222`로 붙는다(WSL `networkingMode=mirrored` 필요). 실행: `python3 tools/flow_browser.py start`(이미 실행 중이면 새로 띄우지 않음), 확인: `python3 tools/flow_browser.py check`. 직접 띄울 때는 `"/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=http://127.0.0.1:9222 '--user-data-dir=C:\Users\houno\AppData\Local\flow-automation\chrome-profile' --no-first-run --no-default-browser-check --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding`.
- 이 프로필은 사용자가 직접 Google에 로그인해 둔 것이다. 로그아웃·쿠키 삭제·프로필 삭제를 하지 않는다. 로그인 만료·캡차는 사용자에게 넘겨 전용 Chrome 창에서 직접 처리하게 한다. 창은 닫지 말고 최소화해 둔다.
- 작업용 Flow 프로젝트 URL을 정해 `FLOW_PROJECT_URL` 환경변수나 `--project-url`로 넘긴다.
- 연결 진단: `python3 tools/flow_run.py --doctor --project-url <URL>`. CDP 연결·Google 로그인·프로젝트 페이지 준비(프롬프트 입력창)를 확인하며, `READY`가 아니면(종료코드 1) 생성하지 않는다. 생성 명령에도 같은 자동 사전 검사가 적용된다.
- Pillow·Playwright는 프로젝트 venv에 둔다: `python3 install.py`가 `pillow==12.0.0`·`playwright==1.65.2`를 설치한다(브라우저 내려받기 불필요). `review.py`는 `.venv/bin/python`으로 실행한다.
- 셀프 테스트: `python3 tools/flow_run.py --self-test`, `.venv/bin/python tools/review.py --self-test`가 둘 다 `PASS`여야 한다.
- 화면을 빼앗지 않는다: 전용 Chrome의 백그라운드 탭에 CDP로 클릭·입력하고 창을 앞으로 가져오지 않는다. 생성은 `nohup ... &`로 백그라운드에서 돌리고 로그 파일만 읽는다.
- 배치 하나는 CDP 연결 하나로 처리한다(옛 `aside repl` 120초 제한 없음). 다운로드는 실행마다 Windows 임시 폴더(`C:\Users\houno\AppData\Local\flow-automation\downloads\run-…`)로 받아 `/mnt/c`로 읽는다. 2K 다운로드는 count 2 이하로 둔다.
- 다운로드는 한 번에 하나만 진행한다. 프로젝트 화면에서 시작된 것으로 확인된 다운로드만 받고, 다운로드가 시작되지 않거나 끝나지 않거나 취소되면 배치를 멈춘다(`fatal: download_unresolved`). 그 항목은 Flow에서 결과를 확인한 뒤 `--force`로만 다시 만들고, 아직 제출하지 않은 항목은 그대로 이어서 실행한다. 다른 탭이나 사용자가 받은 파일은 가져가거나 지우지 않고, 남은 파일은 실행 끝에 `staging_leftovers`로 알려 준다.
- 전용 Chrome에서 사람이 직접 다운로드하지 않는다. 같은 프로젝트 화면의 수동 다운로드가 자동 다운로드 순간과 겹치면 구분할 수 없어 그림이 뒤바뀔 수 있다(2026-11-16 사용자 결정으로 감수한 위험).

## 1. 기획 (Claude)
- 일관성 기준: 캐릭터/제품마다 "고정 블록"(외형·의상·팔레트·조명·렌즈·화풍)을 한 번 정하고 모든 프롬프트 앞에 그대로 붙인다. 장면별로 바뀌는 것은 행동·배경·구도만 둔다.
- 기준 이미지가 필요하면 먼저 1~4장을 생성해 하나를 고르고, 그 타일의 Flow 제목(`manifest.json`의 `titles`)을 `ref_title`로 쓴다.
- `prompts.jsonl` 한 줄 형식: `{"id":"s001","prompt":"...","ratio":"9:16","count":2,"ref_title":"..."}`. `ratio`·`count`·`ref_title`·`quality`(`"1k"`|`"2k"`)는 생략할 수 있다(명령 기본값 사용). 프롬프트는 영어가 안정적이다.

## 2. 생성
```
python3 tools/flow_run.py prompts.jsonl --project-url <URL> --ratio 1:1 --count 2 --quality 1k
```
- 같은 `--run <dir>`로 다시 실행하면 `done` 항목은 건너뛰고 나머지만 이어서 한다. 강제로 다시 만들 때는 `--force`.
- 제출 여부가 불명확한 항목은 자동 재제출하지 않고 종료코드 3으로 알린다. 먼저 Flow에서 결과를 확인한다. `--force`는 완료된 항목도 다시 만들므로, 필요한 항목만 담은 입력 파일로 범위를 제한한 뒤 사용한다. 다시 받은 이미지는 기존 파일을 덮지 않고 `a_1.v2.jpg`처럼 새 이름으로 저장된다.
- 1080 결과물이 필요하면 `--quality 2k`로 받는다(1K는 1080보다 작다).
- 종료코드 2는 치명 정지다(`fatal` 이름: `transport_unavailable`·`login_required`·`flow_not_ready`·`settings_mismatch`·`transport_error`·`problem_text`·`incomplete_item`·`download_unresolved`). `manifest.json`의 `fatal`과 `logs/`를 확인하고, 원인이 풀리기 전에는 다시 실행하지 않는다.
- 연결 실패 시 다른 이미지 모델이나 유료 API로 자동 전환하지 않는다. 실패 원인을 보고하고 대체 모델 사용은 사용자 승인 뒤에만 한다.
- 운영 한도: 한 번 실행에 최대 280개 프롬프트(`--max-items`), 프롬프트 사이 5~12초, 30개마다 1~3분 휴식(기본값). 기본값을 낮추지 않는다.

## 3. 자동 검사
```
.venv/bin/python tools/review.py check <run>
```
`FAIL` 사유(누락·손상·작은 크기·빈 이미지·중복)가 있는 항목은 판정 없이 바로 재생성 목록에 넣는다.

## 4. 몽타주
```
.venv/bin/python tools/review.py montage <run>            # 전체
.venv/bin/python tools/review.py montage <run> --ids s003,s010   # 일부
```

## 5. 판정 (Claude)
- `montage_NN.jpg`만 본다. 원본 이미지를 한 장씩 열지 않는다.
- 항목별로 합격/불합격과 사유(일관성 이탈·손·글자·구도·화질)를 정하고, 불합격은 프롬프트를 고쳐 `redo.jsonl`에 쓴다. 같은 `id`에 `_2` 같은 접미사를 붙여 새 id로 만든다.
- 수정이 필요한 합격 후보는 그 타일 제목을 `ref_title`로 붙이고 바꿀 점만 적은 프롬프트로 `redo.jsonl`에 넣는다(참조 기반 수정).

## 6. 재생성
`redo.jsonl`로 2단계를 같은 `--run`에 다시 실행 → 3~5단계 반복. 같은 장면이 두 번 연속 불합격하면 프롬프트 방향을 바꾸거나 사용자에게 보고한다.

## 7. 납품
```
.venv/bin/python tools/review.py fit <run> --size 1080x1920   # 또는 1080x1080, 1920x1080
```
합격한 항목만 `final_<WxH>/`에서 전달한다. 원본은 고치지 않는다.
