# Provider Usage Abstraction Design

## 1. ProviderUsage Data Structure
To accommodate the diverse usage/limit reporting capabilities of various providers (e.g., Anthropic's detailed windows vs. providers with no API), we propose the following `ProviderUsage` data structure.

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any

@dataclass
class ProviderUsage:
    provider: str               # e.g., 'claude', 'codex', 'gemini'
    status: str                 # See section 2 below (active, stale, loading, unsupported, error)
    
    # Optional fields (must gracefully handle None if the provider doesn't supply them)
    plan: str | None = None                 # User's plan/tier, e.g., "Pro", "Free"
    utilization: float | None = None        # Primary quota utilization percentage (0.0 to 100.0)
    resets_at: datetime | None = None       # When the current quota resets
    
    fetched_at: datetime | None = None      # When this data was last successfully fetched
    
    # Provider-specific extras (e.g., multiple Claude limit windows)
    raw_data: dict[str, Any] | None = None
```

## 2. Explicit States
It is critical to distinguish between the following 5 states:
1. `active`: Data was successfully fetched and is fresh.
2. `stale`: We have previous data, but the latest fetch failed or is backed off (e.g., due to rate limits).
3. `loading`: Data is currently being fetched for the first time.
4. `unsupported`: The provider does not offer a usage API or limit system.
5. `error`: Fetch failed (e.g., network error, not logged in, auth token expired) and no stale data is available.

**Why 0% is dangerous if mixed:**
If an `unsupported` or `error` state is mistakenly rendered as `0%` utilization, it tells the user "you have 100% of your quota left." The user will proceed under the false assumption of infinite quota, leading to a jarring hard stop when the provider unexpectedly rate-limits or denies their request. `None` or "Unknown" must render as a distinct symbol (like `—`), never as `0%`.

## 3. UX Design

### Desktop (Status Header)
The desktop status header has limited horizontal space, and showing 6+ provider limits simultaneously would be cluttered.
- **Normal state:** Display a minimal unified usage indicator. For example, show small provider icons. If a provider's utilization exceeds a warning threshold (e.g., 80%), add a yellow/red dot to its icon.
- **Detailed view:** Hovering over the unified indicator or specific provider icon reveals a rich tooltip showing the exact `plan`, `utilization` bar, `status`, and `resets_at`. `stale` data should be visually subdued (e.g., grayed out text with a "Last updated X mins ago" note). `unsupported` providers simply state "Usage not tracked".

### Remote PWA (Mobile Screen)
Mobile screens lack hover interactions and horizontal space.
- **Normal state:** A single "Limits" button/icon in the header. If any active provider is near its limit, this icon turns warning-yellow/red.
- **Detailed view:** Tapping the icon opens a bottom drawer or modal. Inside, stack each provider vertically in cards. Each card clearly shows the provider name, a progress bar for `utilization`, and the reset time.

## 4. Refresh Policy
- **Network Backoff:** Providers that use network requests (like Claude's internal OAuth usage endpoint) MUST respect HTTP 429 `Retry-After` headers. If rate-limited, the system must set a `backoff_until` timestamp and skip all polling for that provider until the backoff expires.
- **Shared State:** Since multiple cockpit instances might run concurrently, the `backoff_until` and `fetched_at` timestamps must be persisted to disk (e.g., a shared JSON file in the config directory). This prevents instances from hammering the API and extending rate-limit penalties.
- **Polling Frequency:** 
  - Do not poll `unsupported` providers.
  - For active providers, poll on a reasonable interval (e.g., every 5-10 minutes) when the cockpit is active, or trigger a fetch right after an AI generation completes, as that is when the usage actually changes.

## 5. Risks and Precautions
- **Internal APIs:** Endpoints like Anthropic's console usage API are often undocumented and subject to schema changes. The parser must be extremely defensive (using `.get()`, wrapping in `try-except`, avoiding raw dictionary indexing) so that a dropped field results in `None` rather than a crash.
- **Token Expiry/Auth Interruption:** Fetching usage requires auth tokens. If the token expires, the poller should gracefully fall back to the `error` state and prompt the user to re-authenticate, rather than infinitely retrying and locking the account.
- **Cross-process Contention:** Polling must use atomic file writes (e.g., write to a `.tmp` file and rename) when updating the shared usage state to prevent file corruption when multiple instances poll simultaneously.
