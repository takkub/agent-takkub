# Tests
Delete OpenViking-only tests.
Rewrite mixed tests to retain generic context/resource behavior.

Add regressions:
1. Cockpit boot imports no OpenViking module
2. no request to 127.0.0.1:1933
3. no OpenViking process spawn
4. no OpenViking Settings page
5. no `ov` product command
6. Brain/Conversation/Graft/local resources still work
7. task-size context gate still works
8. old env/runtime directory cannot break startup
9. #362 untouched
