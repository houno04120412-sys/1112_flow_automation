# Flow 나노바나나2 대량 생성 자동화 — 작업 기록

## 목표
- Google Flow(https://flow.google.com/)의 무료 모델 나노바나나2로 이미지를 대량 생성한다(계정 1개, 하루 약 400장 한도 안).
- 반복 구간(프롬프트 조합·브라우저 조작·다운로드·정리·자동 검사·몽타주)은 Python/JS 툴이 컴퓨터 자원으로 처리하고, AI 토큰은 기획과 최종 품질 판정에만 쓴다.
- 일관성 확보·품질 상승·이미지 수정을 에이전트가 툴로 수행한다.

## 제약
- 계정 차단 회피: 실제 로그인한 브라우저에서 화면 요소를 조작하는 방식(라핀과 같은 원리). Flow 내부 API 직접 호출·여러 계정 순환은 하지 않는다.
- 라핀 확장(`D:\0 download\라핀 lapin-flow-generator_automate_플로우 오토메이트-v1.2.zip`)은 수정 금지 라이선스·난독화 → 원리만 참고하고 새로 만든다.
- 이 작업은 Claude만 사용한다(다른 모델 위임 없음).
- 사용자 실사용 근거: 라핀으로 한 번에 30개 이상 배치, 제재 없음, 속도 양호.

## 작업 순서
1. 작업 순서 기록(이 파일)
2. 사전 조사
   - 2-1. 로그인된 브라우저로 Flow 화면 접근 확인(aside repl)
   - 2-2. Flow UI 구조·동작 조사: 입력창, 생성 버튼, 나노바나나2 선택, 장수·비율 설정, 참조 이미지(애셋), 편집, 다운로드, 오류·한도 메시지
   - 2-3. 공개 Flow 자동화 자료(오픈소스·확장) 조사
3. 기술 검증 스파이크: 코드로 프롬프트 1~3개 입력→생성→결과 이미지 URL 확보까지 확인, 자동화 방식(크롬 확장 vs Playwright/CDP) 결정
4. PRD 작성(`docs/PRD.md`)
5. ─ 여기까지 진행 후 사용자 확인 ─ (앱 제작은 승인 뒤)
6. 본 제작: 생성기 → 프롬프트 조합 툴 → 자동 검사·몽타주 툴 → 재생성 루프, 툴마다 self-test
7. 실전 검증: 30장 이상 배치로 속도·실패율·일관성 측정, 기본값 조정

## 현재 상태
- 2026-11-16 Aside 제거·CDP 전환: Aside REPL은 탭 목록만 되고 부착·열기가 `TypeError: fetch failed`(이전 CLI·`aside exec`도 동일)라 버렸다. 대신 Flow 전용 Windows Chrome(프로필 `C:\Users\houno\AppData\Local\flow-automation\chrome-profile`, 사용자가 직접 Google 로그인, CDP `127.0.0.1:9222`)을 WSL의 `playwright==1.65.2`가 `connect_over_cdp`로 조작한다(WSL mirrored 네트워크). 새 `tools/flow_browser.py`(전송 계층·`start|check`), `flow_run.py`는 Aside 분기 제거. 다운로드는 Playwright Download API가 CDP에서 `canceled`라 `Browser.setDownloadBehavior`로 Windows 임시 폴더에 받아 `/mnt/c`로 읽는다.
- 중단 안전성: 제출 가능성이 있는 항목은 `submission_unresolved`로 기록돼 done 전까지 유지되고, 자동 재제출하지 않고 종료코드 3으로 차단한다. `--force`만 재생성하며 기존 이미지는 `a_1.v2.jpg`처럼 새 이름으로 보존한다.
- 검증: 회귀 테스트 21개·self-test PASS. 실전 `artifacts/runs/20261116-cdp-e2e1` 3/3 done(1K 1024², 참조 생성, 2K 2048²), review check PASS, 재개 skip·doctor READY 확인. 독립 검토 최종 PASS(회귀 34개, self-test PASS, 실전 `artifacts/runs/20261116-cdp-e2e2`). 다운로드는 사용자 결정에 따라 한 번에 하나만: 프레임·호스트로 확인된 다운로드만 받고, 시작·완료가 늦거나 취소되면 배치를 멈춘다(`download_unresolved`). 감수한 위험: 같은 프로젝트 화면에서 사람이 직접 받는 다운로드가 겹치면 구분 불가. `python3 install.py`로 전역 반영, 커밋은 미요청.
- 연결 끊김 대응 원칙: 연결 실패 시 다른 이미지 모델로 자동 전환하지 않는다(0929_medical.ai.book에서 Flow 실패 후 gpt-image-2로 임의 대체된 사례).
- 1~4단계 완료. 6단계(본 제작) 1차 완료: 스크립트 2개 + 절차 문서. 인계는 `HANDOFF.md`.
- 결정: GUI 앱 없이 절차 문서 + 보조 스크립트 2개로 축소(compose는 Claude가 jsonl 직접 작성, check·montage·fit은 review.py 하나). 브라우저 실행기는 `aside repl` 1차, Playwright CDP 대체안.
- 도구:
  - `tools/flow_run.py` — 프롬프트 jsonl → Flow 생성·다운로드·manifest, 재실행 시 done skip — `python tools/flow_run.py p.jsonl --project-url <URL> --count 2 --quality 1k`
  - `tools/review.py` — check(손상·크기·빈 이미지·중복)·montage·fit(1080 맞춤) — `python tools/review.py check|montage|fit <run>`
  - 절차: `docs/WORKFLOW.md`
  - 전역 스킬 `flow-image` — "플로우로 제작" 등에 자동 적용 — 원본 `skills/flow-image/SKILL.md`, 설치·갱신 `python3 install.py`(스킬을 ~/.agents·~/.claude·~/.mixdog/data/skills에 복사, 런처 `~/.agents/bin/flow-run`·`flow-review` 설치)
- 실전 시험(2026-11-12, `artifacts/runs/20261112-flowrun-test2`): t01 x2 생성 29초·다운로드 2장, t02 `ref_title` 참조 수정 35초(원본 화풍 유지), check PASS, 몽타주 육안 일치.
- 고친 결함: 새 타일을 DOM 순서로 찾으면 옛 이미지를 받음 → 생성 전후 img src 차이로 식별 / 타일 표시(data-fr)가 hover 재렌더로 사라짐 → 사용 직전 재표시 / 메뉴는 그룹 안 `button[aria-label="옵션 더보기"]` 직접 클릭 / PowerShell BOM jsonl → utf-8-sig.
- 측정: x1 3건 연속 제출 26~36초 완료, x2 26초, 참조 수정 32초, 1K 다운로드 1024² JPEG.
- 해상도: 1K 1:1 1024², 16:9 1376×768 / 2K 1:1 2048², 16:9 2752×1536(약 16초) → 1080은 2K 받아 축소.
- 조사 산출물: `artifacts/runs/20261112-flow-ui-probe/`(probe*.txt, 샘플 이미지).
- 2026-11-13 WSL 이전·보완: Pillow는 `.venv`(pillow==12.0.0), review는 `.venv/bin/python`. 화면 비점유 실측(비활성 탭+DevTools 조작, 전면 창·커서 불변). `aside repl` 호출이 120초에 강제 종료 → 프롬프트 1개 = 호출 1회(`--chunk 1`, 호출 예산 105초, 간격은 Python에서). REPL은 매번 새 세션이라 쿠키 배너가 생성 버튼을 덮음 → 보일 때만 "나중에" 클릭. "reCAPTCHA 로 보호됨"은 상시 문구라 오류 감지에서 제외. dHash 64→256비트, dup 기준 10(다른 장면 최소 21, 재인코딩·2K 최대 4).
- 실전 시험(2026-11-13, `artifacts/runs/20261113-consistency10`): 기준 c01 생성 후 c02~c10을 `ref_title`로 참조, 10/10 done, 장당 25~43초, check PASS, 몽타주상 캐릭터·그림체·마을 일관. `artifacts/runs/20261113-916-2k`: 9:16 2K 1536×2752 → fit 1080×1920 PASS.
- 미해결: 한도·오류·캡차 실제 문구(아직 발생 안 함), 무료 한도 공식 수치.
- 다음 행동: (1) 30개 이상 배치로 실패율 측정, 한도 문구가 나오면 `problemLines` 보강 (2) 같은 캐릭터 x2 이상 배치의 합격률 측정.

## 이력
- 2026-11-13: WSL에서 재개. 120초 호출 제한·쿠키 배너·중복 판정 결함 수정, 일관성 10장 시험 성공.
- 2026-11-12: 라핀 v1.2 분석(DOM 매크로: textbox insertText → 생성 버튼 클릭 → 이미지 감지 → 다운로드, 5~10초 무작위 대기, 애셋 검색으로 캐릭터 참조). 작업 순서 확정.
