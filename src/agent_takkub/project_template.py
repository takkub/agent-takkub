"""New-project template: what the user can pick, and the scaffold it writes.

Replaces the old "✨ โปรเจคใหม่ (AI rules)" flow, which ran ``claude -p``
headless for up to two minutes to write a CLAUDE.md. That tied project setup
to one provider. Here every choice is a click and the files are rendered
from the choices — no model call, so every provider works the same.

Provider-neutral rules: the scaffold writes ``AGENTS.md`` as the one rules
file (codex, agy, opencode and cursor read it natively) and a one-line
``CLAUDE.md`` that imports it (``@AGENTS.md``) for claude.

Only the skeleton is written — folders, a README per part naming the stack
and the generator command, ``docker-compose.yml`` for the chosen data
services (app services sit in the ``app`` profile until they have a
Dockerfile), ``.env.example``, ``.gitignore``. Framework generators
(create-next-app, nest new, …) change often and need the network, so a pane
runs them when the work starts. Nothing existing is ever overwritten.

Pure module: no Qt. The dialog lives in ``new_project_dialog.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

NONE = "ไม่ใช้"


@dataclass(frozen=True)
class SubGroup:
    id: str
    label: str
    options: tuple[str, ...]
    swatch: bool = False


@dataclass(frozen=True)
class Layer:
    id: str
    title: str
    dir: str
    hint: str
    options: tuple[tuple[str, str], ...]  # (name, tag shown next to it)
    subs: tuple[SubGroup, ...] = ()
    multi: bool = False
    required: bool = False  # no "ไม่ใช้" chip


STYLE_OPTS = (
    "Tailwind CSS",
    "CSS Modules",
    "Sass / SCSS",
    "styled-components",
    "Emotion",
    "UnoCSS",
    "Vanilla Extract",
    "Bootstrap",
    "CSS ล้วน",
)
KIT_OPTS = (
    "shadcn/ui",
    "MUI",
    "Ant Design",
    "Chakra UI",
    "Mantine",
    "DaisyUI",
    "HeroUI",
    "Radix UI",
    "Vuetify (Vue)",
    "Element Plus (Vue)",
    "PrimeVue (Vue)",
    NONE,
)
ICON_OPTS = ("Lucide", "Heroicons", "Font Awesome", "Material Icons", "Tabler Icons", "Iconify")
SWATCH: dict[str, str] = {
    "Indigo": "#6366F1",
    "Blue": "#3B82F6",
    "Emerald": "#10B981",
    "Teal": "#14B8A6",
    "Orange": "#F97316",
    "Rose": "#F43F5E",
    "Violet": "#8B5CF6",
    "Slate": "#64748B",
    "Amber": "#F59E0B",
}


def _o(*names: str) -> tuple[tuple[str, str], ...]:
    return tuple((n, "") for n in names)


LAYERS: tuple[Layer, ...] = (
    Layer(
        "web",
        "หน้าเว็บ",
        "web/",
        "เลือก 1",
        _o(
            "Next.js",
            "React + Vite",
            "Nuxt (Vue)",
            "SvelteKit",
            "Angular",
            "Astro",
            "Remix",
            "HTML ล้วน",
        ),
        (
            SubGroup("webstyle", "สไตล์ (CSS)", STYLE_OPTS),
            SubGroup("webui", "ชุด component (UI kit)", KIT_OPTS),
            SubGroup("webicon", "ไอคอน", ICON_OPTS),
            SubGroup(
                "webstate",
                "จัดการ state / ดึงข้อมูล",
                ("TanStack Query", "Zustand", "Redux Toolkit", "Pinia (Vue)", "SWR", "Jotai", NONE),
            ),
        ),
    ),
    Layer(
        "admin",
        "หลังบ้าน (admin)",
        "admin/",
        "เลือก 1",
        _o("Next.js", "React + Vite", "Refine", "React-Admin", "Nuxt (Vue)"),
        (
            SubGroup("adminstyle", "สไตล์ (CSS)", STYLE_OPTS),
            SubGroup("adminui", "ชุด component (UI kit)", KIT_OPTS),
        ),
    ),
    Layer(
        "api",
        "API",
        "api/",
        "เลือก 1",
        (
            ("NestJS", "Node"),
            ("ElysiaJS", "Bun"),
            ("Hono", "Bun/Node"),
            ("Express", "Node"),
            ("Fastify", "Node"),
            ("FastAPI", "Python"),
            ("Django", "Python"),
            ("Laravel", "PHP"),
            ("Go Fiber", "Go"),
            ("Go Gin", "Go"),
            (".NET", "C#"),
            ("Spring Boot", "Java"),
        ),
        (
            SubGroup(
                "orm",
                "ORM / ตัวต่อ DB",
                (
                    "Prisma",
                    "Drizzle",
                    "TypeORM",
                    "Sequelize",
                    "Mongoose",
                    "SQLAlchemy",
                    "Django ORM",
                    "Eloquent",
                    "GORM",
                    "EF Core",
                    "JPA",
                    NONE,
                ),
            ),
            SubGroup("apistyle", "รูปแบบ API", ("REST", "GraphQL", "tRPC", "gRPC")),
        ),
    ),
    Layer(
        "db",
        "ฐานข้อมูล",
        "db/",
        "เลือก 1",
        _o("PostgreSQL", "MySQL", "MariaDB", "MongoDB", "SQLite", "SQL Server"),
    ),
    Layer(
        "mobile",
        "มือถือ",
        "mobile/",
        "เลือก 1",
        _o(
            "Expo (React Native)",
            "Flutter",
            "LINE LIFF",
            "Ionic",
            "Kotlin (Android)",
            "Swift (iOS)",
        ),
        (
            SubGroup(
                "mobilestyle",
                "สไตล์ / UI",
                (
                    "NativeWind (Tailwind)",
                    "Tamagui",
                    "React Native Paper",
                    "Gluestack UI",
                    "Material 3 (Flutter)",
                    "Cupertino (Flutter)",
                    "Tailwind (LIFF)",
                    NONE,
                ),
            ),
        ),
    ),
    Layer(
        "worker",
        "งานเบื้องหลัง (queue / cron)",
        "worker/",
        "เลือก 1",
        _o("BullMQ", "Celery", "Temporal", "ใช้ cron อย่างเดียว"),
    ),
    Layer(
        "theme",
        "ธีม / หน้าตา",
        "",
        "ใช้กับทุกส่วนที่มีหน้าจอ (web / admin / mobile)",
        _o(
            "Minimal สะอาด",
            "Corporate องค์กร",
            "Modern SaaS",
            "Glassmorphism",
            "Neo-brutalism",
            "Material Design",
            "iOS / Apple",
            "Dashboard มืด",
            "สดใส ขี้เล่น",
        ),
        (
            SubGroup("mode", "โหมด", ("มืด + สว่าง (สลับได้)", "สว่างอย่างเดียว", "มืดอย่างเดียว")),
            SubGroup("color", "สีหลัก", tuple(SWATCH), swatch=True),
            SubGroup(
                "font",
                "ฟอนต์ (รองรับไทย)",
                (
                    "Noto Sans Thai",
                    "IBM Plex Sans Thai",
                    "Prompt",
                    "Kanit",
                    "Sarabun",
                    "Anuphan",
                    "Inter + Noto Sans Thai",
                ),
            ),
            SubGroup("radius", "ความโค้งมุม", ("เหลี่ยม", "โค้งนิด", "โค้งมาก")),
        ),
        required=True,
    ),
    Layer(
        "lang",
        "ภาษาฝั่ง JS/TS",
        "",
        "ใช้กับ web / admin / api ที่เป็น JS",
        _o("TypeScript", "JavaScript"),
        required=True,
    ),
    Layer("pm", "ตัวจัดการแพ็กเกจ", "", "", _o("pnpm", "npm", "bun", "yarn"), required=True),
    Layer(
        "extras",
        "บริการเสริม",
        "",
        "ติ๊กได้หลายอัน",
        _o(
            "Redis",
            "MinIO (S3)",
            "Meilisearch",
            "Elasticsearch",
            "RabbitMQ",
            "Kafka",
            "Keycloak (login)",
            "Mailpit (ทดสอบอีเมล)",
            "nginx",
            "Adminer / pgAdmin",
        ),
        multi=True,
    ),
    Layer(
        "tools",
        "อื่นๆ ในโปรเจค",
        "",
        "ติ๊กได้หลายอัน",
        _o(
            "docker-compose",
            "Playwright e2e",
            "GitHub Actions CI",
            "packages/shared",
            "docs/",
            "scripts/",
            "Terraform",
            "Storybook",
        ),
        multi=True,
    ),
)
LAYER_BY_ID = {layer.id: layer for layer in LAYERS}

THEME_DEFAULT: dict = {
    "theme": "Modern SaaS",
    "mode": "มืด + สว่าง (สลับได้)",
    "color": "Indigo",
    "font": "IBM Plex Sans Thai",
    "radius": "โค้งนิด",
}
_WEB = {
    "webstyle": "Tailwind CSS",
    "webui": "shadcn/ui",
    "webicon": "Lucide",
    "webstate": "TanStack Query",
}
PRESETS: dict[str, dict] = {
    "Web + API + DB": {
        "web": "Next.js",
        **_WEB,
        "api": "NestJS",
        "orm": "Prisma",
        "apistyle": "REST",
        "db": "PostgreSQL",
        "lang": "TypeScript",
        "pm": "pnpm",
        "extras": [],
        "tools": ["docker-compose", "Playwright e2e"],
    },
    "SaaS เต็มชุด": {
        "web": "Next.js",
        **_WEB,
        "admin": "Refine",
        "adminstyle": "Tailwind CSS",
        "adminui": "Ant Design",
        "api": "NestJS",
        "orm": "Prisma",
        "apistyle": "REST",
        "db": "PostgreSQL",
        "worker": "BullMQ",
        "lang": "TypeScript",
        "pm": "pnpm",
        "extras": ["Redis", "MinIO (S3)", "Keycloak (login)", "Mailpit (ทดสอบอีเมล)"],
        "tools": [
            "docker-compose",
            "Playwright e2e",
            "GitHub Actions CI",
            "packages/shared",
            "docs/",
        ],
    },
    "LINE LIFF": {
        "mobile": "LINE LIFF",
        "mobilestyle": "Tailwind (LIFF)",
        "admin": "Next.js",
        "adminstyle": "Tailwind CSS",
        "adminui": "shadcn/ui",
        "api": "ElysiaJS",
        "orm": "Drizzle",
        "apistyle": "REST",
        "db": "PostgreSQL",
        "lang": "TypeScript",
        "pm": "bun",
        "extras": ["Redis"],
        "tools": ["docker-compose", "Playwright e2e"],
    },
    "Mobile app": {
        "mobile": "Expo (React Native)",
        "mobilestyle": "NativeWind (Tailwind)",
        "api": "NestJS",
        "orm": "Prisma",
        "apistyle": "REST",
        "db": "PostgreSQL",
        "lang": "TypeScript",
        "pm": "pnpm",
        "extras": ["MinIO (S3)"],
        "tools": ["docker-compose", "packages/shared"],
    },
    "API อย่างเดียว": {
        "api": "FastAPI",
        "orm": "SQLAlchemy",
        "apistyle": "REST",
        "db": "PostgreSQL",
        "lang": "TypeScript",
        "pm": "pnpm",
        "extras": ["Redis"],
        "tools": ["docker-compose", "GitHub Actions CI"],
    },
    "Landing page": {
        "web": "Astro",
        "webstyle": "Tailwind CSS",
        "webui": "shadcn/ui",
        "webicon": "Lucide",
        "webstate": NONE,
        "lang": "TypeScript",
        "pm": "pnpm",
        "extras": [],
        "tools": ["GitHub Actions CI"],
    },
    "ว่างเปล่า": {"lang": "TypeScript", "pm": "pnpm", "extras": [], "tools": []},
}
DEFAULT_PRESET = "Web + API + DB"

# part id → projects.json path key (config._ROLE_PATH_PREFS reads these)
_PART_KEYS = ("web", "admin", "mobile", "api", "worker")


def preset(name: str) -> dict:
    sel = dict(THEME_DEFAULT)
    for k, v in PRESETS[name].items():
        sel[k] = list(v) if isinstance(v, list) else v
    return sel


def default_selection() -> dict:
    return preset(DEFAULT_PRESET)


def _has(sel: dict, key: str) -> bool:
    return bool(sel.get(key)) and sel.get(key) != NONE


def _join(*parts: object) -> str:
    return " + ".join(str(p) for p in parts if p and p != NONE)


def has_screens(sel: dict) -> bool:
    return any(_has(sel, k) for k in ("web", "admin", "mobile"))


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "project"


# ── what the right-hand panel shows ──────────────────────────────────────


def tree_entries(name: str, sel: dict) -> list[tuple[str, str, bool]]:
    """(label, comment, is_dir) rows of the folder tree, root first."""
    tools = sel.get("tools") or []
    rows: list[tuple[str, str, bool]] = [(f"{name}/", "", True)]

    def add(label: str, comment: str = "", is_dir: bool = True) -> None:
        rows.append((label, comment, is_dir))

    if _has(sel, "web"):
        add("web/", sel["web"])
    if _has(sel, "admin"):
        add("admin/", sel["admin"])
    if _has(sel, "mobile"):
        add("mobile/", sel["mobile"])
    if _has(sel, "api"):
        add("api/", _join(sel["api"], sel.get("orm")))
    if _has(sel, "worker"):
        add("worker/", sel["worker"])
    if _has(sel, "db"):
        add("db/", sel["db"])
        add("  migrations/")
        add("  seed/")
    if "packages/shared" in tools:
        add("packages/shared/", "types ใช้ร่วม")
    if "Playwright e2e" in tools:
        add("e2e/", "Playwright")
    if "Terraform" in tools:
        add("infra/", "Terraform")
    if "docs/" in tools:
        add("docs/")
    if "scripts/" in tools:
        add("scripts/")
    if "GitHub Actions CI" in tools:
        add(".github/workflows/", "ci.yml")
    if "docker-compose" in tools:
        add("docker-compose.yml", "", False)
    add(".env.example", "", False)
    add(".gitignore", "", False)
    add("README.md", "", False)
    add("AGENTS.md", "rules กลาง ทุก provider อ่าน", False)
    add("CLAUDE.md", "มีแค่ @AGENTS.md", False)
    return rows


# image, container port, env var holding the host port, extra env, volume mount
_DB_SERVICES: dict[str, tuple[str, int, str, dict[str, str], str]] = {
    "PostgreSQL": (
        "postgres:17-alpine",
        5432,
        "DB_PORT",
        {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app"},
        "/var/lib/postgresql/data",
    ),
    "MySQL": (
        "mysql:8.4",
        3306,
        "DB_PORT",
        {"MYSQL_ROOT_PASSWORD": "app", "MYSQL_DATABASE": "app"},
        "/var/lib/mysql",
    ),
    "MariaDB": (
        "mariadb:11",
        3306,
        "DB_PORT",
        {"MARIADB_ROOT_PASSWORD": "app", "MARIADB_DATABASE": "app"},
        "/var/lib/mysql",
    ),
    "MongoDB": ("mongo:7", 27017, "DB_PORT", {}, "/data/db"),
    "SQL Server": (
        "mcr.microsoft.com/mssql/server:2022-latest",
        1433,
        "DB_PORT",
        {"ACCEPT_EULA": "Y", "MSSQL_SA_PASSWORD": "App_passw0rd"},
        "/var/opt/mssql",
    ),
}
# extra → (service name, image, [(container port, host env, default host)], env, command, volume)
_EXTRA_SERVICES: dict[
    str, tuple[str, str, list[tuple[int, str, int]], dict[str, str], str, str]
] = {
    "Redis": ("redis", "redis:7-alpine", [(6379, "REDIS_PORT", 6379)], {}, "", "/data"),
    "MinIO (S3)": (
        "minio",
        "minio/minio:latest",
        [(9000, "MINIO_PORT", 9000), (9001, "MINIO_CONSOLE_PORT", 9001)],
        {"MINIO_ROOT_USER": "minio", "MINIO_ROOT_PASSWORD": "minio12345"},
        'server /data --console-address ":9001"',
        "/data",
    ),
    "Meilisearch": (
        "meilisearch",
        "getmeili/meilisearch:v1.10",
        [(7700, "MEILI_PORT", 7700)],
        {"MEILI_MASTER_KEY": "dev-master-key"},
        "",
        "/meili_data",
    ),
    "Elasticsearch": (
        "elasticsearch",
        "docker.elastic.co/elasticsearch/elasticsearch:8.15.0",
        [(9200, "ES_PORT", 9200)],
        {"discovery.type": "single-node", "xpack.security.enabled": "false"},
        "",
        "/usr/share/elasticsearch/data",
    ),
    "RabbitMQ": (
        "rabbitmq",
        "rabbitmq:3-management",
        [(5672, "RABBITMQ_PORT", 5672), (15672, "RABBITMQ_UI_PORT", 15672)],
        {},
        "",
        "/var/lib/rabbitmq",
    ),
    "Kafka": ("kafka", "apache/kafka:3.8.0", [(9092, "KAFKA_PORT", 9092)], {}, "", ""),
    "Keycloak (login)": (
        "keycloak",
        "quay.io/keycloak/keycloak:25.0",
        [(8080, "KEYCLOAK_PORT", 8081)],
        {"KEYCLOAK_ADMIN": "admin", "KEYCLOAK_ADMIN_PASSWORD": "admin"},
        "start-dev",
        "",
    ),
    "Mailpit (ทดสอบอีเมล)": (
        "mailpit",
        "axllent/mailpit:latest",
        [(1025, "SMTP_PORT", 1025), (8025, "MAILPIT_UI_PORT", 8025)],
        {},
        "",
        "",
    ),
    "nginx": ("nginx", "nginx:alpine", [(80, "NGINX_PORT", 8088)], {}, "", ""),
    "Adminer / pgAdmin": ("adminer", "adminer:latest", [(8080, "ADMINER_PORT", 8089)], {}, "", ""),
}
_APP_PORTS = {"web": 3000, "admin": 3001, "api": 4000}


def compose_services(sel: dict) -> list[str]:
    """Service names docker-compose.yml will hold (empty without docker-compose)."""
    if "docker-compose" not in (sel.get("tools") or []):
        return []
    out: list[str] = []
    if _has(sel, "db") and sel["db"] in _DB_SERVICES:
        out.append("db")
    out += [k for k in ("api", "web", "admin", "worker") if _has(sel, k)]
    out += [_EXTRA_SERVICES[e][0] for e in sel.get("extras") or [] if e in _EXTRA_SERVICES]
    return out


def rules_lines(sel: dict) -> list[str]:
    """One line per decision — the AGENTS.md summary and the dialog preview."""
    r: list[str] = []
    if _has(sel, "web"):
        extra = ""
        if _has(sel, "webicon"):
            extra += f" · ไอคอน {sel['webicon']}"
        if _has(sel, "webstate"):
            extra += f" · {sel['webstate']}"
        r.append(
            f"web = {_join(sel['web'], sel.get('webstyle'), sel.get('webui'))} อยู่ที่ web/{extra}"
        )
    if _has(sel, "admin"):
        r.append(
            f"admin = {_join(sel['admin'], sel.get('adminstyle'), sel.get('adminui'))} อยู่ที่ admin/"
        )
    if _has(sel, "mobile"):
        r.append(f"mobile = {_join(sel['mobile'], sel.get('mobilestyle'))} อยู่ที่ mobile/")
    if has_screens(sel):
        r.append(
            f"ธีม {sel.get('theme')} · {sel.get('mode')} · สีหลัก {sel.get('color')}"
            f" · ฟอนต์ {sel.get('font')} · มุม{sel.get('radius')}"
        )
    if _has(sel, "api"):
        style = f" ({sel['apistyle']})" if _has(sel, "apistyle") else ""
        orm = f" · {sel['orm']}" if _has(sel, "orm") else ""
        r.append(f"api = {sel['api']}{style}{orm} อยู่ที่ api/")
    if _has(sel, "db"):
        r.append(f"db = {sel['db']} · migration อยู่ที่ db/migrations")
    if _has(sel, "worker"):
        r.append(f"worker = {sel['worker']} อยู่ที่ worker/")
    r.append(f"ภาษา {sel.get('lang') or 'TypeScript'} · ใช้ {sel.get('pm') or 'pnpm'}")
    if "docker-compose" in (sel.get("tools") or []):
        r.append("รัน local ด้วย docker compose up -d (cockpit down ให้เองตอนจบงาน)")
    return r


def summary_counts(sel: dict) -> tuple[int, int, int]:
    parts = sum(1 for k in ("web", "admin", "api", "db", "mobile", "worker") if _has(sel, k))
    return parts, len(sel.get("extras") or []), len(sel.get("tools") or [])


# ── files ─────────────────────────────────────────────────────────────────

_PM_CREATE = {
    "pnpm": "pnpm create",
    "npm": "npm create",
    "bun": "bun create",
    "yarn": "yarn create",
}
_PM_X = {"pnpm": "pnpm dlx", "npm": "npx", "bun": "bunx", "yarn": "yarn dlx"}


def generator_hint(framework: str, sel: dict) -> str:
    """Suggested command to scaffold *framework* inside its folder ('' = see docs)."""
    pm = sel.get("pm") or "pnpm"
    create, x = _PM_CREATE.get(pm, "npm create"), _PM_X.get(pm, "npx")
    ts = (sel.get("lang") or "TypeScript") == "TypeScript"
    hints = {
        "Next.js": f"{create} next-app@latest .",
        "React + Vite": f"{create} vite@latest . -- --template {'react-ts' if ts else 'react'}",
        "Nuxt (Vue)": f"{x} nuxi@latest init .",
        "SvelteKit": f"{x} sv create .",
        "Angular": f"{x} @angular/cli new <name> --directory .",
        "Astro": f"{create} astro@latest .",
        "Remix": f"{x} create-react-router@latest .",
        "Refine": f"{create} refine-app@latest .",
        "React-Admin": f"{create} react-admin@latest .",
        "NestJS": f"{x} @nestjs/cli new . --package-manager {pm}",
        "ElysiaJS": "bun create elysia .",
        "Hono": f"{create} hono@latest .",
        "Expo (React Native)": f"{create} expo-app@latest .",
        "Flutter": "flutter create .",
        "Ionic": f"{x} @ionic/cli start",
        "Django": "django-admin startproject app .",
        "Laravel": "composer create-project laravel/laravel .",
    }
    return hints.get(framework, "")


def _part_readme(title: str, stack: str, framework: str, sel: dict) -> str:
    hint = generator_hint(framework, sel)
    how = (
        f"สร้างโค้ดเริ่มต้นใน folder นี้ด้วย:\n\n```\n{hint}\n```\n\n(เช็คกับเอกสารล่าสุดของ {framework} ก่อนรัน)"
        if hint
        else f"สร้างโค้ดเริ่มต้นตามเอกสารล่าสุดของ {framework}"
    )
    return f"# {title} — {stack}\n\nยังเป็นโครงว่างจาก template ของ agent-takkub\n\n{how}\n"


def _compose(name: str, sel: dict) -> tuple[str, dict[str, str]]:
    """(docker-compose.yml text, host-port env var → default)."""
    lines = [f"name: {slug(name)}", "", "services:"]
    volumes: list[str] = []
    env_vars: dict[str, str] = {}
    if _has(sel, "db") and sel["db"] in _DB_SERVICES:
        image, port, env_key, env, mount = _DB_SERVICES[sel["db"]]
        env_vars[env_key] = str(port)
        lines += ["  db:", f"    image: {image}", "    restart: unless-stopped"]
        if env:
            lines.append("    environment:")
            lines += [f"      {k}: {v}" for k, v in env.items()]
        lines += ["    ports:", f'      - "${{{env_key}:-{port}}}:{port}"']
        lines += ["    volumes:", f"      - db-data:{mount}"]
        volumes.append("db-data")
    for part in ("api", "web", "admin", "worker"):
        if not _has(sel, part):
            continue
        lines += [
            f"  {part}:",
            "    # เปิดใช้หลังมี Dockerfile ใน folder นี้: docker compose --profile app up -d",
            '    profiles: ["app"]',
            f"    build: ./{part}",
            "    env_file: .env",
        ]
        if part in _APP_PORTS:
            key = f"{part.upper()}_PORT"
            env_vars[key] = str(_APP_PORTS[part])
            lines += ["    ports:", f'      - "${{{key}:-{_APP_PORTS[part]}}}:{_APP_PORTS[part]}"']
        if _has(sel, "db") and sel["db"] in _DB_SERVICES:
            lines += ["    depends_on:", "      - db"]
    for extra in sel.get("extras") or []:
        if extra not in _EXTRA_SERVICES:
            continue
        svc, image, ports, env, command, mount = _EXTRA_SERVICES[extra]
        lines += [f"  {svc}:", f"    image: {image}", "    restart: unless-stopped"]
        if command:
            lines.append(f"    command: {command}")
        if env:
            lines.append("    environment:")
            lines += [f'      {k}: "{v}"' for k, v in env.items()]
        lines.append("    ports:")
        for cport, key, host in ports:
            env_vars[key] = str(host)
            lines.append(f'      - "${{{key}:-{host}}}:{cport}"')
        if mount:
            vol = f"{svc}-data"
            lines += ["    volumes:", f"      - {vol}:{mount}"]
            volumes.append(vol)
    if len(lines) == 3:
        lines.append("  {}")
    if volumes:
        lines += ["", "volumes:"] + [f"  {v}: {{}}" for v in volumes]
    return "\n".join(lines) + "\n", env_vars


def _env_example(name: str, sel: dict, ports: dict[str, str]) -> str:
    out = [f"# {name} — copy เป็น .env แล้วแก้ค่าจริง (ห้าม commit .env)", ""]
    if ports:
        out += ["# port ฝั่งเครื่อง (เปลี่ยนได้ถ้าชนกับโปรเจคอื่น — แก้ URL ด้านล่างให้ตรงด้วย)"]
        out += [f"{k}={v}" for k, v in ports.items()]
        out.append("")
    db = sel.get("db") if _has(sel, "db") else ""
    db_port = ports.get("DB_PORT") or (str(_DB_SERVICES[db][1]) if db in _DB_SERVICES else "")
    urls = {
        "PostgreSQL": f"postgresql://app:app@localhost:{db_port}/app",
        "MySQL": f"mysql://root:app@localhost:{db_port}/app",
        "MariaDB": f"mysql://root:app@localhost:{db_port}/app",
        "MongoDB": f"mongodb://localhost:{db_port}/app",
        "SQL Server": f"sqlserver://localhost:{db_port};database=app;user=sa;password=App_passw0rd",
        "SQLite": "file:./db/app.sqlite",
    }
    if db:
        out.append(f"DATABASE_URL={urls.get(db, '')}")
    if "Redis" in (sel.get("extras") or []):
        out.append(f"REDIS_URL=redis://localhost:{ports.get('REDIS_PORT', '6379')}")
    return "\n".join(out) + "\n"


_GITIGNORE = """node_modules/
.env
.env.*
!.env.example
dist/
build/
.next/
.nuxt/
.output/
coverage/
__pycache__/
*.pyc
.venv/
vendor/
.DS_Store
"""


def _agents_md(name: str, sel: dict) -> str:
    rows: list[tuple[str, str, str]] = []
    if _has(sel, "web"):
        rows.append(("หน้าเว็บ", "web/", _join(sel["web"], sel.get("webstyle"), sel.get("webui"))))
    if _has(sel, "admin"):
        rows.append(
            ("หลังบ้าน", "admin/", _join(sel["admin"], sel.get("adminstyle"), sel.get("adminui")))
        )
    if _has(sel, "mobile"):
        rows.append(("มือถือ", "mobile/", _join(sel["mobile"], sel.get("mobilestyle"))))
    if _has(sel, "api"):
        rows.append(("API", "api/", _join(sel["api"], sel.get("apistyle"), sel.get("orm"))))
    if _has(sel, "db"):
        rows.append(("ฐานข้อมูล", "db/", sel["db"]))
    if _has(sel, "worker"):
        rows.append(("งานเบื้องหลัง", "worker/", sel["worker"]))
    md = [
        f"# {name}",
        "",
        "> rules ของโปรเจคนี้ — claude, codex, agy, opencode และ cursor อ่านไฟล์นี้ทุกตัว "
        "(CLAUDE.md แค่ import ไฟล์นี้) แก้ rules ที่นี่ที่เดียว",
        "",
        "## โครงสร้าง",
        "",
    ]
    if rows:
        md += ["| ส่วน | folder | stack |", "|---|---|---|"]
        md += [f"| {a} | `{b}` | {c} |" for a, b, c in rows]
    else:
        md.append("_(ยังไม่ได้เลือกส่วนไหน)_")
    if has_screens(sel):
        md += [
            "",
            "## ธีม / หน้าตา",
            "",
            f"- แนวดีไซน์: {sel.get('theme')}",
            f"- โหมด: {sel.get('mode')}",
            f"- สีหลัก: {sel.get('color')} (`{SWATCH.get(str(sel.get('color')), '')}`)",
            f"- ฟอนต์: {sel.get('font')}",
            f"- ความโค้งมุม: {sel.get('radius')}",
        ]
        if _has(sel, "webicon"):
            md.append(f"- ไอคอน: {sel['webicon']}")
        if _has(sel, "webstate"):
            md.append(f"- state / ดึงข้อมูล: {sel['webstate']}")
        md.append("- ทุกหน้าจอใช้ธีมชุดเดียวกัน ห้ามใส่สีหรือฟอนต์ตรงๆ ในแต่ละ component ให้ใช้ token ของธีม")
    extras = sel.get("extras") or []
    md += [
        "",
        "## ภาษาและเครื่องมือ",
        "",
        f"- ภาษาฝั่ง JS/TS: {sel.get('lang') or 'TypeScript'}",
        f"- ตัวจัดการแพ็กเกจ: {sel.get('pm') or 'pnpm'} (ใช้ตัวนี้ตัวเดียว ห้ามปน lockfile)",
    ]
    if extras:
        md.append(f"- บริการเสริม: {', '.join(extras)}")
    tools = sel.get("tools") or []
    if tools:
        md.append(f"- อื่นๆ: {', '.join(tools)}")
    md += ["", "## รัน local", ""]
    if "docker-compose" in tools:
        md += [
            "- `docker compose up -d` เปิดฐานข้อมูลและบริการเสริม",
            "- service ของแอปอยู่ใน profile `app` เปิดด้วย `docker compose --profile app up -d` "
            "หลังมี Dockerfile ในแต่ละ folder",
            "- cockpit down stack นี้ให้เองตอน end-session / ปิดแท็บ (volume ยังอยู่)",
        ]
    md.append("- ค่าตั้งต้นอยู่ใน `.env.example` ให้ copy เป็น `.env` ก่อนรัน (ห้าม commit `.env`)")
    md += [
        "",
        "## ข้อตกลง",
        "",
        "- โค้ดของแต่ละส่วนอยู่ใน folder ของส่วนนั้นเท่านั้น ของที่ใช้ร่วมกันไว้ที่ `packages/shared/`"
        if "packages/shared" in tools
        else "- โค้ดของแต่ละส่วนอยู่ใน folder ของส่วนนั้นเท่านั้น",
        "- folder ยังเป็นโครงว่าง README ในแต่ละ folder บอกคำสั่งสร้างโค้ดเริ่มต้น",
        "- เปลี่ยน stack เมื่อไรให้แก้ตารางด้านบนในไฟล์นี้ด้วย",
    ]
    if _has(sel, "db"):
        md.append("- schema เปลี่ยนผ่าน migration ใน `db/migrations/` เท่านั้น")
    return "\n".join(md) + "\n"


@dataclass
class ScaffoldResult:
    root: Path
    created: list[Path] = field(default_factory=list)
    kept: list[Path] = field(default_factory=list)  # already existed — untouched


def scaffold_files(name: str, sel: dict) -> dict[str, str]:
    """relative path → content for every file the template writes."""
    files: dict[str, str] = {}
    tools = sel.get("tools") or []
    titles = {"web": "web", "admin": "admin", "mobile": "mobile", "api": "api", "worker": "worker"}
    stacks = {
        "web": _join(sel.get("web"), sel.get("webstyle"), sel.get("webui")),
        "admin": _join(sel.get("admin"), sel.get("adminstyle"), sel.get("adminui")),
        "mobile": _join(sel.get("mobile"), sel.get("mobilestyle")),
        "api": _join(sel.get("api"), sel.get("apistyle"), sel.get("orm")),
        "worker": str(sel.get("worker") or ""),
    }
    for part, title in titles.items():
        if _has(sel, part):
            files[f"{part}/README.md"] = _part_readme(title, stacks[part], str(sel[part]), sel)
    if _has(sel, "db"):
        files["db/README.md"] = (
            f"# db — {sel['db']}\n\n- `migrations/` ไฟล์ migration เรียงตามเวลา\n- `seed/` ข้อมูลตั้งต้นสำหรับ dev\n"
        )
        files["db/migrations/.gitkeep"] = ""
        files["db/seed/.gitkeep"] = ""
    for tool, path, body in (
        (
            "packages/shared",
            "packages/shared/README.md",
            "# packages/shared\n\ntypes และโค้ดที่ทุกส่วนใช้ร่วมกัน\n",
        ),
        ("Playwright e2e", "e2e/README.md", "# e2e — Playwright\n\nเทสแบบผู้ใช้จริง ข้ามทุกส่วน\n"),
        ("Terraform", "infra/README.md", "# infra — Terraform\n"),
        ("docs/", "docs/README.md", f"# docs — {name}\n"),
        ("scripts/", "scripts/README.md", "# scripts\n\nสคริปต์ช่วย dev / deploy\n"),
    ):
        if tool in tools:
            files[path] = body
    if "GitHub Actions CI" in tools:
        files[".github/workflows/ci.yml"] = (
            "name: ci\non:\n  push:\n  pull_request:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n"
            "      # TODO: เพิ่ม install / lint / test ของแต่ละส่วนหลังสร้างโค้ดเริ่มต้นแล้ว\n"
        )
    ports: dict[str, str] = {}
    if "docker-compose" in tools:
        files["docker-compose.yml"], ports = _compose(name, sel)
    files[".env.example"] = _env_example(name, sel, ports)
    files[".gitignore"] = _GITIGNORE
    files["README.md"] = (
        f"# {name}\n\n"
        + "\n".join(f"- {line}" for line in rules_lines(sel))
        + "\n\nrules ของทีมอยู่ที่ `AGENTS.md`\n"
    )
    files["AGENTS.md"] = _agents_md(name, sel)
    files["CLAUDE.md"] = "@AGENTS.md\n"
    return files


def write_scaffold(root: Path, name: str, sel: dict) -> ScaffoldResult:
    """Create *root* and the template files. An existing file is never
    overwritten (it lands in ``kept``)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    result = ScaffoldResult(root=root)
    for rel, content in scaffold_files(name, sel).items():
        target = root / rel
        if target.exists():
            result.kept.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        result.created.append(target)
    return result


def project_paths(root: Path, sel: dict) -> dict[str, str]:
    """projects.json ``paths``: ``main`` = root, then one key per code part
    (the keys `config._ROLE_PATH_PREFS` routes roles by)."""
    root = Path(root).resolve()
    paths = {"main": root.as_posix()}
    for part in _PART_KEYS:
        if _has(sel, part):
            paths[part] = (root / part).as_posix()
    return paths
