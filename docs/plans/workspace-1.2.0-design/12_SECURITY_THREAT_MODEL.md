# Security Threat Model

File editor risks: traversal, symlink escape, binary/device reads, secret exposure, concurrent overwrite, huge files.
Mitigate with canonical containment, allowlisted roots, limits, conflict hashes, atomic writes, background IO.

Preview risks: arbitrary remote page + privileged bridge, `file://` lateral reads, surprise navigation.
Mitigate by no privileged bridge on arbitrary pages, strict artifact roots, navigation policy, profile isolation if needed.

External design content = untrusted context unless user-approved.
OpenViking stays optional sidecar; do not vendor AGPL source into MIT repo.
