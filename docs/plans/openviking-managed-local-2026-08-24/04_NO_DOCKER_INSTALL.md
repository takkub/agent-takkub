# No-Docker Install

Preferred: dedicated managed Python environment owned by Takkub.

Windows:
`%USERPROFILE%\.agent-takkub\services\openviking\`

macOS/Linux:
`~/.agent-takkub/services/openviking/`

Contents:
- venv/
- config/
- data/
- logs/
- state.json

Flow:
1. create venv
2. install tested/pinned `openviking`
3. verify `openviking-server`
4. configure
5. doctor
6. record version

Do not install OpenViking dependencies into Takkub's PyQt venv unless unavoidable.
Do not vendor OpenViking source into the MIT repo.
