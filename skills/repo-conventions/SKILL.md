---
name: repo-conventions
description: Template quy ước riêng của repo (build, test, style, layout) để worker tuân theo. Copy vào .agents/skills/repo-conventions/SKILL.md của dự án rồi sửa cho đúng.
---

# Repo conventions

> Đây là **template**. `coord init` không copy file này. Khi dùng cho một dự án thật:
>
> ```bash
> mkdir -p .agents/skills/repo-conventions
> cp <đường-dẫn-tới>/skills/repo-conventions/SKILL.md .agents/skills/repo-conventions/SKILL.md
> ```
>
> Worker Manager tự đọc mọi `.agents/skills/*/SKILL.md` trong worktree và chèn vào system prompt
> của worker.

## Build

```bash
# ví dụ
uv sync
```

## Test

```bash
# ví dụ
uv run pytest -q
```

## Style

- Formatter / linter: (điền)
- Quy ước đặt tên file, module: (điền)

## Layout

- Code chính: `src/`
- Test: `tests/`
- Không sửa: (sinh tự động, vendor, generated)

## Điều cấm trong repo này

- (ví dụ: không đổi schema DB, không bump dependency major, không commit file sinh tự động)
