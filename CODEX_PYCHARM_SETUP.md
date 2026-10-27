# Codex + PyCharm — Terra Medium + Sol Max

**Default:** GPT-6.1 Terra Medium. **Escalation reviewer:** GPT-6.1 Sol Max,
only for safety-related IR feedback control, sensor data validity, interface
writes, numerical stability, complex architecture and release-critical changes.

## Setup (Windows / PyCharm)
1. Open/clone this GitHub repository in PyCharm. For an existing local clone:
   `git pull --ff-only` (first inspect `git status` for local changes).
2. Open PyCharm **Terminal** in the repository root.
3. Install/update Codex CLI, log in and launch:
   ```powershell
   npm install -g @openai/codex@latest
   codex --version
   codex login
   codex
   ```
4. Accept the project trust prompt to load `.codex/config.toml`. Use
   `/status` to inspect the model and effort; use `/subagents` to inspect
   whether the Sol reviewer actually ran.
5. Example routine (Terra Medium) prompt:
   `CSV 조회 그래프 UI를 개선해줘. 기존 제어 수식은 수정하지 말고 테스트해.`
6. Example conditional escalation (Sol Max) prompt:
   `layer_algorithm.py와 csv_source.py의 오래된 센서값 처리와 제어 안정성을 sol_reviewer에게 검토하게 해줘. 실제 히터 또는 장비는 변경하지 마.`

## Important limitations
- `AGENTS.md` only instructs Codex when to use the custom reviewer; an
  automatic subagent call is not guaranteed. Verify in the Codex session.
- The configured Sol model and max effort must be available to the signed-in
  account/workspace. If unavailable, report review incomplete; do not silently
  replace the model or lower effort.
- PyCharm Terminal + Codex CLI is the intended execution path. Codex
  CLI/IDE may share supported config layers but a separate IDE plugin
  should not be presumed configured merely because the repo is updated.
- Current app warning: the -1 invalid sensor marker is rejected, but
  apparently normal stale CSV sensor data cannot always be differentiated.
  The repository has no completed real S3 PLC/sensor/lamp integration
  validation. Start offline/read-only; do not enable automatic real-device
  application without dedicated validation and authorization.
- Changes here configure coding agents only; they do NOT change the live
  feedback controller, heater, PLC, binaries or deployment.

## 실제 검토 완료 확인과 인증 실패

[공통 검토 절차](CODEX_REVIEW_PROTOCOL.md)를 따릅니다. /subagents의 호출 표시뿐 아니라 실제 코드·시험을 읽은 검토자의 최종 응답과 지적 사항 처리 결과를 확인합니다. 401/451·토큰 갱신 실패·모델/Max 미지원·응답 누락은 검토 미완료입니다. 로그인 상태 표시만으로 성공으로 처리하지 않습니다.

같은 Sol Max를 제공하는 정식 협업 도구로 실제 검토가 성공하면 해당 경로를 기록할 수 있지만 CLI 인증 복구와 구분합니다. 모델·추론 강도를 임의로 낮추지 않습니다. 복구되지 않으면 일반 코딩과 오프라인 모의 검증만 진행하고, 필수 검토 대상의 최종 승인·장비 적용·배포는 보류합니다. 사용자의 별도 보류 지시는 검토가 성공해도 유지합니다.
