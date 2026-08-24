# Architecture

```text
Takkub Cockpit
  └ OpenVikingManager
      ├ installer
      ├ managed venv
      ├ config
      ├ process supervisor
      ├ health monitor
      ├ logs
      └ openviking-server
           └ 127.0.0.1:<port>
```

Suggested:
`src/agent_takkub/openviking/{manager,installer,process,config,diagnostics,settings_dialog}.py`

Keep the HTTP-client module (openviking_adapter, removed in 1.6.0) as the HTTP client.
Core must not import PyQt/UI manager.
