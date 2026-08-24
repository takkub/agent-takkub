# Performance Rules

Hard:
- no heavy FS/git/network on Qt main thread
- one Monaco WebView app-wide
- one Preview WebView app-wide
- lazy create/destroy
- discard hidden renderer where safe
- debounced file watcher
- debounced Git status
- bounded diff/file sizes
- no per-file OS watcher explosion
- no OpenViking call on GUI thread

Acceptance:
- existing +300 MB workspace budget remains
- close returns WebEngine processes to baseline
- project switching remains smooth
- large repo lazy explorer remains responsive
