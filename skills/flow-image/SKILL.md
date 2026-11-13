---
name: flow-image
description: Google Flow(flow.google.com)의 무료 모델 Nano Banana 2.1로 이미지를 생성·대량 생성한다. "플로우로 제작", "플로우로 만들어", "플로우에서 생성", "Flow로 이미지", "나노바나나로 생성"처럼 Flow·나노바나나 이미지 제작을 요청받으면 반드시 사용. 로그인된 Aside 브라우저를 화면·마우스·키보드를 빼앗지 않고 백그라운드로 조작하며, 캐릭터·그림체·공간 일관성 시리즈, 참조 이미지 수정, 자동 검사·몽타주·1080 납품까지 포함. 영상(Veo)·유료 이미지 생성은 해당 없음.
---

# Flow 이미지 제작 (Nano Banana 2.1)

원본 저장소: `~/project_wsl/1112_flow_automation` (도구 소스, `docs/WORKFLOW.md`·`docs/PRD.md`에 상세·실측값). 갱신은 그 저장소에서 `git pull` 후 `python3 install.py`.

## 도구 (설치 위치 고정)
| 명령 | 역할 |
|---|---|
| `~/.agents/bin/flow-run` | 프롬프트 jsonl → Flow 생성·다운로드·`manifest.json`. 같은 `--run`으로 다시 실행하면 `done` 항목은 건너뜀 |
| `~/.agents/bin/flow-review` | `check`(누락·손상·크기·빈 이미지·중복) / `montage`(번호 붙은 축소 그리드) / `fit`(1080 규격 맞춤) |

없거나 `--self-test`가 `PASS`가 아니면 `python3 ~/project_wsl/1112_flow_automation/install.py`.

## 준비 (세션당 1회)
1. `~/.agents/bin/flow-run --self-test`, `~/.agents/bin/flow-review --self-test` 둘 다 `PASS`.
2. 작업용 Flow 프로젝트 URL: 사용자가 주면 그것, 없으면 기본 시험 프로젝트 `https://flow.google.com/project/e274e3f2-100b-4579-a090-f3c7198d97a9`. `export FLOW_PROJECT_URL=<URL>` 또는 `--project-url`.
3. 로그인은 사용자의 Aside 브라우저 세션을 쓴다. `flow_not_ready`(로그인 만료 등)가 나면 사용자에게 Aside 브라우저에서 flow.google.com 로그인을 요청한다.

## 절차
1. **기획(모델 판단)**: 고정 블록(그림체·팔레트·캐릭터 외형·의상·공간)을 한 번 정해 모든 프롬프트에 그대로 넣고, 장면마다 행동·배경·구도만 바꾼다. 영어 프롬프트가 안정적이다.
2. **기준 이미지**: 일관성 시리즈면 기준 1장을 먼저 생성한다(전신, 공간 포함). `manifest.json`의 `titles[0]`(Flow 자동 제목)이 참조 이름이다.
3. **장면 생성**: 나머지 줄에 `"ref_title":"<기준 제목>"`과 `Keep the exact same girl/character, outfit and art style as the reference image.`를 넣는다.
   한 줄 형식: `{"id":"c02","prompt":"...","ratio":"1:1","count":1,"ref_title":"..."}` (`ratio` 16:9·4:3·1:1·3:4·9:16, `count` 1~4).
   ```
   ~/.agents/bin/flow-run prompts.jsonl --run artifacts/runs/<YYYYMMDD-HHMMSS>-<작업명> [--quality 2k]
   ```
   긴 배치는 `nohup ~/.agents/bin/flow-run ... > <run>/run.log 2>&1 &`로 백그라운드 실행하고 `manifest.json`으로 진행을 본다.
4. **검사 게이트**: `~/.agents/bin/flow-review check <run>` → `FAIL` 항목은 판정 없이 재생성 목록으로.
5. **몽타주 판정**: `~/.agents/bin/flow-review montage <run>` → `montage_NN.jpg`만 본다(원본을 한 장씩 열지 않음). 불합격은 새 id(`c03_2`)로 `redo.jsonl`에 넣어 같은 `--run`으로 다시 실행. 같은 장면 2회 연속 불합격이면 프롬프트 방향을 바꾸거나 보고.
6. **납품**: 1080 규격이 필요하면 생성 때 `--quality 2k`(count 2 이하)로 받고 `~/.agents/bin/flow-review fit <run> --size 1080x1920|1080x1080|1920x1080`. 1K 원본은 1080보다 작아 `fit`이 거부한다(정상). 15절 폴더 구조 프로젝트면 합격본을 `export/<MMDD>/`에 `_1` 버전 이름으로 복사한다.

## 동작 사실 (2026-11-13 실측)
- 화면 비점유: 비활성 탭 + DevTools 프로토콜 조작이라 사용자 마우스·키보드·전면 창이 바뀌지 않는다.
- 장당 25~43초(1K x1), 2K 9:16은 1536×2752. 프롬프트 1개 = `aside repl` 호출 1회(호출당 120초 강제 종료 대응).
- 해상도: 1K 1:1 1024², 16:9 1376×768 / 2K 1:1 2048², 16:9 2752×1536.

## 규칙
- 계정 1개, 사용자의 실제 로그인 브라우저에서만. Flow 내부 API 직접 호출 금지. 기본 간격(5~12초)·휴식(30개마다 1~3분)·`--max-items 280`을 낮추지 않는다.
- 종료코드 2(로그인 만료·설정 불일치·오류/한도 문구·aside 호출 실패)면 멈추고 `manifest.json`의 `fatal`을 사용자에게 알린다. 원인이 풀리기 전 재실행 금지.
- 화면 문구가 바뀌어 실패하면 원본 저장소 `tools/flow_run.py`의 `JS_TEMPLATE`만 고치고 `--self-test` 후 커밋한다.
