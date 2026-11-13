# 에이전트 절차 — Flow 나노바나나 대량 생성

에이전트는 이 순서만 따른다. 반복 작업은 스크립트가 하고, 토큰은 1단계(기획)와 5단계(몽타주 판정)에만 쓴다.

## 0. 준비 (세션당 1회)
- Aside 브라우저에서 `https://flow.google.com/`에 로그인돼 있어야 한다. 로그인·캡차는 사용자에게 넘긴다.
- 작업용 Flow 프로젝트 URL을 정해 `FLOW_PROJECT_URL` 환경변수나 `--project-url`로 넘긴다.
- Pillow는 프로젝트 venv에 둔다: `python3 -m venv .venv && .venv/bin/pip install pillow==12.0.0`. `review.py`는 `.venv/bin/python`으로 실행한다.
- 셀프 테스트: `python3 tools/flow_run.py --self-test`, `.venv/bin/python tools/review.py --self-test`가 둘 다 `PASS`여야 한다.
- 화면을 빼앗지 않는다: `flow_run`은 Aside 브라우저에 비활성 탭을 열고 DevTools 프로토콜로 클릭·입력하므로 사용자 마우스·키보드·전면 창이 바뀌지 않는다(2026-11-13 실측). 생성은 `nohup ... &`로 백그라운드에서 돌리고 로그 파일만 읽는다.
- `aside repl` 한 번 호출은 120초에 강제 종료되므로 프롬프트 하나를 호출 한 번으로 처리한다(`--chunk 1` 기본). 2K 다운로드는 count 2 이하로 둔다.

## 1. 기획 (Claude)
- 일관성 기준: 캐릭터/제품마다 "고정 블록"(외형·의상·팔레트·조명·렌즈·화풍)을 한 번 정하고 모든 프롬프트 앞에 그대로 붙인다. 장면별로 바뀌는 것은 행동·배경·구도만 둔다.
- 기준 이미지가 필요하면 먼저 1~4장을 생성해 하나를 고르고, 그 타일의 Flow 제목(`manifest.json`의 `titles`)을 `ref_title`로 쓴다.
- `prompts.jsonl` 한 줄 형식: `{"id":"s001","prompt":"...","ratio":"9:16","count":2,"ref_title":"..."}`. `ratio`·`count`·`ref_title`은 생략할 수 있다(명령 기본값 사용). 프롬프트는 영어가 안정적이다.

## 2. 생성
```
python3 tools/flow_run.py prompts.jsonl --project-url <URL> --ratio 1:1 --count 2 --quality 1k
```
- 같은 `--run <dir>`로 다시 실행하면 `done` 항목은 건너뛰고 나머지만 이어서 한다. 강제로 다시 만들 때는 `--force`.
- 1080 결과물이 필요하면 `--quality 2k`로 받는다(1K는 1080보다 작다).
- 종료코드 2는 치명 정지(로그인 만료·설정 불일치·오류/한도 문구 감지·aside 호출 실패)다. `manifest.json`의 `fatal`과 `logs/`를 확인하고, 원인이 풀리기 전에는 다시 실행하지 않는다.
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
