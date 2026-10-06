# 작업 0002 — 첫 실사용 피드백 반영

- 지시: 지휘부 · 2026-10-06 밤 (사용자: "4건 포함해서 모니파드 개선 작업 지금 시작")
- 출처: 다른 프로젝트 에이전트의 실사용 보고(2026-10-06, RTX 4090 SECURE). 이 문서에 필요한 사실은 아래에 요약 — 원문 대화 불필요.

## 실사용에서 일어난 일 (요약)
- `quote`가 GPU 10개 조합 전부 `OK to start`를 냈지만 실제 생성 가능한 건 4090·L4뿐. COMMUNITY로 4090 2회·A5000 1회 연속 실패(`no longer any instances available`), SECURE로 1회에 성공.
- 재고를 확인할 수단이 없자 그 에이전트가 `moni_pod/.env`의 키로 REST·GraphQL을 직접 조회(지시 위반, 자진 보고). GraphQL `gpuTypes { lowestPrice(input:{gpuCount:1}) {...} }`에서 가용 없는 GPU는 `lowestPrice`가 비어 있었다(관찰, 공식 의미는 확인 필요).
- 다른 프로젝트 폴더에서 `moni-pod` 실행 시 `RUNPOD_API_KEY not found` — cwd가 moni_pod가 아니면 `.env`를 못 찾음.
- 좋았던 점: quote/status의 "정지 시 디스크 $X/day", 잔액·Auto-Pay 경고는 판단에 직접 도움.

## 할 일 (우선순위 순)
1. **[높음·버그] `.env` 위치** — cwd와 무관하게 플러그인 루트(`${CLAUDE_PLUGIN_ROOT}`/패키지 위치) 기준으로 `.env`를 찾는다. 환경변수 `RUNPOD_API_KEY`가 있으면 그것 우선. 다른 폴더에서 실행하는 테스트 추가.
2. **[높음] 견적에 재고 반영** — `quote`에 실시간 가용 여부를 표시(COMMUNITY/SECURE 각각). 가용 정보를 얻는 **공식** 경로를 먼저 문서로 확인(REST v2에 있으면 그것, 없으면 GraphQL — GraphQL은 퇴역 예정이니 ADR에 기록). 가용 확인이 불가능하면 문구를 `OK (price only — stock not checked)`로 바꾼다. 가용 없음이면 시작 전에 막고 대안(같은 GPU SECURE, 다른 GPU) 제시.
3. **[높음] 조회 전용 명령** — `/moni-pod:gpu-list`(또는 `moni-pod list`): GPU별 VRAM·COMMUNITY/SECURE 가격·가용 여부·이 계정 잔액 기준 최대 사용 가능 시간. 토큰 불필요(읽기 전용), 모델이 호출해도 됨. **에이전트가 키를 꺼내 직접 API를 부를 이유를 없애는 것이 목적.**
4. **[중간] SECURE 대체 안내** — COMMUNITY 생성 실패(재고 없음) 시 같은 사양 SECURE 견적을 바로 보여 주고, 사용자가 다시 `/moni-pod:gpu-start`로 고르게 한다(자동 전환 금지 — 단가가 2배 이상일 수 있음).
5. **[중간·문서] 가드의 한계 명시** — README/readme-notes에: 같은 OS 사용자 권한의 에이전트는 `.env`를 읽을 수 있다 → 잠금은 "지출 호출 차단"이지 "키 사용 차단"이 아니다. 조회 명령(3)이 우회 유인을 줄이는 장치임을 함께. (선택) 에이전트에게 보여 줄 안내 문구: "키를 직접 쓰지 말고 /moni-pod:gpu-list를 쓰라".

## 범위 밖
- 키 파일을 에이전트에게서 숨기는 OS 수준 격리(별도 사용자·자격 증명 관리자) — 검토만 하고 ADR에 대안으로 기록, 구현은 안 함.
- M6(공개) — 이번 작업 범위 아님.

## 규칙
- 실과금 호출(Pod 생성)은 매번 사용자 승인. 이번 작업 실과금 상한 **$1** (가용 조회는 무료 API로 검증, 생성 테스트가 꼭 필요하면 CPU·TTL 5분).
- headless `claude -p` 실험은 꼭 필요할 때만, 비용 보고.
- 끝나면 지휘부 세션에 보고: 항목별 결과 / pytest 숫자 / 다른 폴더 실행 확인 / 지출.
