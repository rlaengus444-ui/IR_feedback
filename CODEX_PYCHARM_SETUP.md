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
- `model` values and `max` effort must be available to the signed-in Codex
  account/workspace; if not, the configuration needs a supported model/effort.
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
