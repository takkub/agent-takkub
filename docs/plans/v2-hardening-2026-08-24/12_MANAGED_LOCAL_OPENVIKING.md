# Managed Local OpenViking

No Docker required.

Takkub should:
- create dedicated managed venv
- install/update/repair OpenViking
- start local openviking-server
- bind 127.0.0.1
- health check
- stop only owned process
- never kill external process
- preserve data/config
- Open Studio button

Disabled => Takkub works normally.
Enabled => user does not manually run terminal commands.
