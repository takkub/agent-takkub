# Project Explorer Final Spec

Required:
- embedded under active project sidebar row
- collapsible
- resizable sidebar
- lazy directory loading
- project-root containment
- symlink/junction escape prevention
- Git-native ignore semantics
- multi-root support
- CHANGES aggregation
- context menu:
  - Open in Takkub
  - Open externally
  - Reveal
  - Copy path
  - Ask Agent
- terminal path click -> internal editor option

Performance:
- no recursive scan on Qt main thread
- cached/lazy roots
- background git
- debounced refresh
