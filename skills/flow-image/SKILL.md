---
name: flow-image
description: Google Flow(flow.google.com)의 무료 모델 Nano Banana 2.1로 이미지를 생성·대량 생성한다. "플로우로 제작", "플로우로 만들어", "플로우에서 생성", "Flow로 이미지", "나노바나나로 생성"처럼 Flow·나노바나나 이미지 제작을 요청받으면 반드시 사용. 로그인된 Flow 전용 Chrome을 Playwright(CDP)로 화면·마우스·키보드를 빼앗지 않고 백그라운드로 조작하며, 캐릭터·그림체·공간 일관성 시리즈, 참조 이미지 수정, 자동 검사·몽타주·1080 납품까지 포함. 영상(Veo)·유료 이미지 생성은 해당 없음.
---

# Flow 이미지 제작 (Nano Banana 2.1)

원본 저장소: `~/project_wsl/1112_flow_automation` (도구 소스, `docs/WORKFLOW.md`·`docs/PRD.md`에 상세·실측값). 갱신은 그 저장소에서 `git pull` 후 `python3 install.py`.

## 도구 (설치 위치 고정)
| 명령 | 역할 |
|---|---|
| `~/.agents/bin/flow-run` | 프롬프트 jsonl → Flow 생성·다운로드·`manifest.json`. 같은 `--run`으로 다시 실행하면 `done` 항목은 건너뜀 |
| `~/.agents/bin/flow-browser` | `start`(전용 Chrome이 꺼져 있을 때만 정해진 플래그로 실행) / `check`(CDP 접속 확인) |
| `~/.agents/bin/flow-review` | `check`(누락·손상·크기·빈 이미지·중복) / `montage`(번호 붙은 축소 그리드) / `fit`(1080 규격 맞춤) |

없거나 `--self-test`가 `PASS`가 아니면 `python3 ~/project_wsl/1112_flow_automation/install.py`.

## 준비 (세션당 1회)
1. `~/.agents/bin/flow-run --self-test`, `~/.agents/bin/flow-review --self-test` 둘 다 `PASS`.
2. 작업용 Flow 프로젝트 URL은 사용자가 지정한 프로젝트를 사용한다. 없으면 프로젝트를 확인하고 `export FLOW_PROJECT_URL=<URL>` 또는 `--project-url`로 전달한다. 다른 사람의 시험 프로젝트를 기본값으로 쓰지 않는다.
3. 브라우저는 Aside가 아니라 Flow 전용 Windows Chrome(프로필 `C:\Users\houno\AppData\Local\flow-automation\chrome-profile`, CDP `127.0.0.1:9222`)이다. 꺼져 있으면 `~/.agents/bin/flow-browser start`. 이 프로필에서 로그아웃·쿠키 삭제·프로필 삭제 금지. `login_required`·`flow_not_ready`(로그인 만료 등)가 나면 사용자에게 작업 표시줄에서 최소화된 전용 Chrome 창을 열어 직접 Google 로그인하도록 요청한다(handoff). 전용 Chrome은 창 없이 시작하고 Flow 탭은 최소화된 창에 열리므로 화면에 뜨거나 포커스를 가져가지 않는다. 헤드리스 모드는 쓰지 않는다(reCAPTCHA·계정 제재 위험).
4. `~/.agents/bin/flow-run --doctor --project-url <URL>`로 CDP 연결·Google 로그인·프로젝트 입력창을 확인한다. 생성 전에도 자동 검사하며, `READY`가 아니면(종료코드 1) 생성하지 않는다.

## 절차
1. **기획(모델 판단)**: 고정 블록(그림체·팔레트·캐릭터 외형·의상·공간)을 한 번 정해 모든 프롬프트에 그대로 넣고, 장면마다 행동·배경·구도만 바꾼다. 영어 프롬프트가 안정적이다.
2. **기준 이미지**: 일관성 시리즈면 기준 1장을 먼저 생성한다(전신, 공간 포함). `manifest.json`의 `titles[0]`(Flow 자동 제목)이 참조 이름이다.
3. **장면 생성**: 나머지 줄에 `"ref_title":"<기준 제목>"`과 `Keep the exact same girl/character, outfit and art style as the reference image.`를 넣는다.
   한 줄 형식: `{"id":"c02","prompt":"...","ratio":"1:1","count":1,"ref_title":"..."}` (`ratio` 16:9·4:3·1:1·3:4·9:16, `count` 1~4, 선택 `"quality":"1k"|"2k"`).
   ```
   ~/.agents/bin/flow-run prompts.jsonl --run artifacts/runs/<YYYYMMDD-HHMMSS>-<작업명> [--quality 2k]
   ```
   긴 배치는 `nohup ~/.agents/bin/flow-run ... > <run>/run.log 2>&1 &`로 백그라운드 실행하고 `manifest.json`으로 진행을 본다.
4. **검사 게이트**: `~/.agents/bin/flow-review check <run>` → `FAIL` 항목은 판정 없이 재생성 목록으로.
5. **몽타주 판정**: `~/.agents/bin/flow-review montage <run>` → `montage_NN.jpg`만 본다(원본을 한 장씩 열지 않음). 불합격은 새 id(`c03_2`)로 `redo.jsonl`에 넣어 같은 `--run`으로 다시 실행. 같은 장면 2회 연속 불합격이면 프롬프트 방향을 바꾸거나 보고.
6. **납품**: 1080 규격이 필요하면 생성 때 `--quality 2k`(count 2 이하)로 받고 `~/.agents/bin/flow-review fit <run> --size 1080x1920|1080x1080|1920x1080`. 1K 원본은 1080보다 작아 `fit`이 거부한다(정상). 15절 폴더 구조 프로젝트면 합격본을 `export/<MMDD>/`에 `_1` 버전 이름으로 복사한다.

## 동작 사실 (2026-11-16 CDP 실측)
- 화면 비점유: 전용 Chrome은 최소화 상태에서 백그라운드 탭 + DevTools 프로토콜로 조작하고, 창을 앞으로 가져오지 않는다(생성 중 포커스 샘플 45회 중 전용 Chrome 0회).
- 장당 21~41초(1K x1·참조 생성·2K 포함), 2K 9:16은 1536×2752. 배치 하나 = CDP 연결 하나.
- 해상도: 1K 1:1 1024², 16:9 1376×768 / 2K 1:1 2048², 16:9 2752×1536.

## 규칙
- 계정 1개, 사용자가 직접 로그인한 전용 Chrome에서만. Flow 내부 API 직접 호출 금지. 기본 간격(5~12초)·휴식(30개마다 1~3분)·`--max-items 280`을 낮추지 않는다.
- 종료코드 2(연결 불가·로그인 만료·설정 불일치·오류/한도 문구)면 멈추고 `manifest.json`의 `fatal`을 사용자에게 알린다. 원인이 풀리기 전 재실행 금지.
- 연결 실패를 이유로 `gpt-image` 등 다른 모델·유료 API로 임의 전환하지 않는다. Flow 작업의 실패 사실과 실제 생성에 사용한 도구를 구분해 보고하고, 대체 모델 사용은 사용자 승인 뒤에만 한다.
- 종료코드 3은 제출 여부가 불명확한 항목을 차단했다는 뜻이다. Flow에서 결과를 확인한 뒤 필요한 항목만 새 입력 파일에 담아 `--force`로 명시적으로 재생성한다. `--force`는 입력에 포함된 완료 항목도 재생성하므로 전체 배치에 무심코 붙이지 않는다. 기존 이미지는 덮지 않고 새 파일은 `a_1.v2.jpg`처럼 저장된다.
- `fatal: download_unresolved`면 다운로드가 늦거나 취소돼 배치가 멈춘 것이다. 그 항목만 Flow에서 결과를 확인하고, 나머지는 같은 `--run`으로 이어서 실행한다. 전용 Chrome에서 사람이 직접 다운로드하지 않는다(자동 다운로드와 겹치면 그림이 뒤바뀔 수 있음).
- 화면 문구가 바뀌어 실패하면 원본 저장소 `tools/flow_browser.py`의 선택자·UI 문구만 고치고 `--self-test` 후 커밋한다.
