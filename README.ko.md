# moni-pod

[English](README.md) | **한국어**

AI 에이전트가 RunPod GPU 요금을 쌓지 못하게 하는 Claude Code 플러그인입니다.
Pod의 시작과 종료는 사용자가 명령으로 직접 하고, 에이전트가 Pod를 만들거나 켤 수 있는 다른 경로는 잠급니다.
각 Pod는 합의한 시간이 지나면 PC가 꺼져 있어도 스스로 정지합니다.

```
/moni-pod:gpu-list 24           # 24 GB 이상 GPU 재고, 시간당 요금, 잔액으로 쓸 수 있는 시간
/moni-pod:gpu-start 4090 2h     # 견적 -> 사용자 확인 -> 2시간 뒤 자동 정지하는 Pod 시작
/moni-pod:gpu-status            # 켜진/정지된 Pod, 남은 시간, 누적 요금, 정지 후에도 나가는 디스크 요금
/moni-pod:gpu-extend pod-1 1h   # 자동 정지 시각 연장 (추가 비용 먼저 확인)
/moni-pod:gpu-stop pod-1        # "결과 파일 회수했나?" -> 정지 또는 삭제 -> 최종 요금과 청구액 대조
```

## 왜 만들었나

에이전트가 대화만으로 클라우드 GPU를 켤 수 있게 되면(RunPod 공식 Claude Code 플러그인이 바로 그 기능입니다),
비용은 더 이상 도구 호출 한 번에 붙어 있지 않습니다. Pod 생성 호출 자체는 $0입니다. 돈은
**Pod가 켜져 있는 시간만큼** 쌓이는데, 그 시간을 관리하는 장치가 없습니다.

| 기존 도구 | 하는 일 | 비어 있는 곳 |
|---|---|---|
| [RunPod 공식 Claude Code 플러그인](https://github.com/runpod/runpod-plugins-official) | 스킬 + 호스팅 MCP: Pod 생성·정지·삭제, 가격·지출 조회 | 에이전트가 스스로 GPU를 켤 수 있고, 지출을 막는 장치는 없음 |
| [Runpod Idle Pod Monitor](https://github.com/runpod/Runpod-Idle-Pod-Monitor) | CPU·GPU·메모리 사용률이 낮게 유지되면 Pod 정지 | 사용률만 봄. 바쁘지만 필요 없는 Pod는 계속 과금되고, 예산·소유자 개념이 없음 |
| 호출 단위 에이전트 예산 가드 | 도구 호출 1회의 비용으로 차단 | 생성 호출은 $0이라 시간에 비례하는 지출은 원리상 못 막음 |
| RunPod `stopAfter` | 정한 시각에 Pod를 정지하려던 기능 | 동작하지 않아 제거됨 ([runpod/runpodctl PR 330](https://github.com/runpod/runpodctl/pull/330)) |
| [aniket-desh/agents](https://github.com/aniket-desh/agents) | RunPod + Claude Code 개인 설정. 사용 한도 장부와 감시 프로세스 | 설치 가능한 플러그인 형태가 아님 |

moni-pod는 이 도구들이 비워 둔 곳, 즉 에이전트가 쓰는 Pod의 **수명**을 맡습니다. 시작과 최대 비용을
사용자가 합의하고, 켜져 있는 동안 지출을 기록하고, 종료와 정산은 사용자가 직접 합니다. 유휴 감지는
만들지 않았습니다. 그 용도로는 공식 Idle Monitor를 쓰면 됩니다.

## 동작 방식

1. **시작과 종료는 사용자가 직접 합니다.** `/gpu-start`, `/gpu-stop`, `/gpu-extend`는 모델이 호출할 수 없습니다.
   사용자가 입력하면 1회용 승인 토큰이 발급되고, 견적을 보여 준 뒤 확인을 받습니다.
2. **훅은 판단하지 않고 잠그기만 합니다.** RunPod MCP, `runpodctl`, 셸에서 직접 부르는 REST/GraphQL을 통한
   생성·시작·수정·삭제는 토큰이 없으면 거부됩니다. 지출이 생기는 호출에는 Claude Code 자체 권한 확인 창도
   한 번 더 뜹니다. `bypassPermissions`, `auto`를 포함한 모든 권한 모드에서 동작하는 것을 확인했습니다.
3. **최후 방어선은 Pod 안에 있습니다.** Pod의 시작 명령을 타이머로 감싸서, 합의한 시간(TTL)이 지나면 Pod가
   스스로 정지합니다. PC나 Claude Code가 꺼져 있어도 동작합니다. 기본 2시간, 상한 8시간, 세션 예산 $2이며
   모두 설정에서 바꿀 수 있습니다.
4. **자동 삭제는 없습니다.** 삭제는 `/gpu-stop`에서 결과 파일을 회수했는지 답한 뒤에만 합니다.
5. **정지해도 요금이 0은 아닙니다.** 정지한 Pod도 디스크 요금이 나갑니다. 상태 화면, 세션 시작 알림,
   상태줄에서 얼마가 얼마 동안 나가고 있는지 보여 줍니다.

개발 중 실제 Pod로 측정한 결과, 장부 추정치와 실제 청구액의 차이는 **0.4%** 였습니다
(Pod 7개, 실제 $0.1003 / 추정 $0.1007). TTL 자동 정지, 정지, 삭제, 재시작, 연장도 각각 실제 Pod로 확인했습니다.

## 설치

필요한 것: [Claude Code](https://claude.com/claude-code), [uv](https://docs.astral.sh/uv/), Python 3.11 이상,
RunPod API 키.

Claude Code에서:

```
/plugin marketplace add reddol18/moni_pod
/plugin install moni-pod@moni-pod
```

키는 moni-pod가 찾을 수 있는 곳에 둡니다. 예: `~/.moni_pod/.env`

```
RUNPOD_API_KEY=your-key
```

찾는 순서: 환경변수 `RUNPOD_API_KEY` → 플러그인 폴더의 `.env` → `~/.moni_pod/.env` → 작업 폴더 →
Claude 프로젝트 폴더.

**RunPod 계정에서는 Auto-Pay를 꺼 두세요.** 선불 크레딧에 Auto-Pay를 끄면 최대 손실은 잔액까지입니다.
잔액이 $0이 되면 RunPod가 Pod를 정지합니다(네트워크 볼륨이 없는 Pod는 삭제됨). RunPod의 지출 한도
(기본 시간당 $80)는 견적과 상태 화면에 참고로만 표시합니다. 사용자가 낮출 수 없는 값입니다
([RunPod 결제 문서](https://docs.runpod.io/accounts-billing/billing)).

### 선택 사항

- **`/gpu-extend`는 대화형 입력 없이 되는 SSH가 필요합니다.** 암호 없는 키나 ssh-agent에 올린 키를 쓰세요.
  로컬 공개키(`~/.ssh/id_ed25519.pub` 등)를 각 Pod에 넣으며 RunPod 계정 설정은 바꾸지 않습니다.
  넣지 않으려면 `--no-local-ssh-key`. Windows에서는 내장 OpenSSH(`C:\Windows\System32\OpenSSH\ssh.exe`)를
  쓰며 `MONI_POD_SSH`로 바꿀 수 있습니다.
- **상태줄** (플러그인이 직접 설정할 수 없어 `~/.claude/settings.json`에 추가):
  ```json
  { "statusLine": { "type": "command", "command": "uv run --quiet --project <plugin dir> moni-pod statusline", "refreshInterval": 60 } }
  ```
  출력 예: `moni_pod: GPU 1 on $0.13/h, next stop 36m | 1 stopped $0.13/day (1 old!)`. 과금 중인 것이 없으면 아무것도 표시하지 않습니다.
- **세션 알림**: 세션이 끝나면 OS 알림(Windows 토스트, macOS, Linux `notify-send`)이 뜹니다. 다음 세션을 시작하면
  켜진 Pod와 디스크 요금이 나가는 정지 Pod 목록을 보여 줍니다. 둘 다 알리기만 하고, 정지나 삭제는 하지 않습니다.

## 한계

알려진 빈틈입니다. 모두 개발 중에 확인했고 [`docs/adr/`](docs/adr)와 [`docs/reports/`](docs/reports)에 기록돼 있습니다.

- 잠금은 에이전트가 보통 쓰는 직접 경로(RunPod MCP, `runpodctl`, 셸 명령 안의 REST/GraphQL)를 잡습니다.
  스크립트 파일·SDK·다른 MCP 클라이언트·웹 콘솔 안의 호출은 보지 못합니다. 그래서 Pod 안의 TTL과 선불 잔액을
  최후 방어선으로 둡니다.
- 잠금은 지출을 막을 뿐 키 읽기는 막지 않습니다. 사용자 계정으로 실행되는 에이전트는 `.env`를 읽을 수 있습니다.
  대신 그럴 이유를 없앴습니다. `/moni-pod:gpu-list`가 키 없이 재고·가격 질문에 답하고, 세션 시작 때 Claude에게
  그렇게 안내합니다. OS 수준의 키 격리는 구현하지 않았습니다([ADR-0006](docs/adr/0006-stock-lookup-and-key-access.md)).
- 셸 리다이렉트로 `~/.moni_pod`에 쓰면 승인 토큰을 위조할 수 있습니다. 그래도 지출 호출마다 뜨는 권한 확인 창은
  막아 줍니다([ADR-0002](docs/adr/0002-lock-mechanism.md)).
- Pod만 다룹니다. 서버리스 엔드포인트와 네트워크 볼륨은 잠그지도 추적하지도 않습니다.
- 추정치는 생성 시점 단가를 쓰며, 커뮤니티 가격은 바뀔 수 있습니다. RunPod 청구 내역은 몇 시간 늦게 올라오므로
  `/gpu-status`는 계정 잔액과 현재 지출 속도를 실시간 기준으로 씁니다.
- TTL은 컨테이너 시작부터 셉니다. 그 전의 이미지 다운로드 시간은 과금될 수 있습니다.
- 재고는 RunPod 카탈로그의 수준(NONE/LOW/MEDIUM/HIGH)이며 예약이 아닙니다. NONE이면 생성 전에 거부하고,
  LOW는 실패할 수 있습니다. COMMUNITY 생성이 실패하면 같은 사양의 SECURE 가격을 보여 주고, 시작할지는
  사용자가 고릅니다.
- 볼륨이 없으면 TTL로 정지될 때 Pod 안의 모든 것이 지워집니다. GPU Pod는 기본으로 20 GB 볼륨을 붙입니다.

## 개발

```
uv run pytest
```

테스트는 목(mock) RunPod 클라이언트를 쓰며 실제 API를 부르지 않습니다. 설계는 [`docs/PLAN.md`](docs/PLAN.md)와
[`docs/adr/`](docs/adr)에, 측정값이 담긴 마일스톤 보고는 [`docs/reports/`](docs/reports)에 있습니다.

## 라이선스

[MIT](LICENSE)
