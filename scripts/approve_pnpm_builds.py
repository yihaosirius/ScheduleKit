"""写 pnpm 的"允许执行构建脚本"白名单。

背景：pnpm 10 起默认**不执行依赖的 postinstall 脚本**（这是对的，
供应链攻击最常藏在那里）。但有些包确实需要构建步骤，跳过就会出问题。

**关键：pnpm 12 的设置位置与键名都变了。** 我连着踩了两次：
  1. 第一次写进 `package.json` 的 `pnpm` 字段 → 警告
     "The 'pnpm' field in package.json is no longer read by pnpm"，无效。
  2. 第二次写进 `pnpm-workspace.yaml` 的 `onlyBuiltDependencies` → **依然无效**，
     pnpm 12 仍然报 `ERR_PNPM_IGNORED_BUILDS`。
     查 pnpm 的 v10→v11 迁移文档才确认：v11 起
     `onlyBuiltDependencies` / `neverBuiltDependencies` /
     `ignoredBuiltDependencies` / `onlyBuiltDependenciesFile` **被合并成了
     单一的 `allowBuilds` 映射**（`{ 包名: true | false }`），设置文件是
     `pnpm-workspace.yaml`。

顺带一提：即使设置了不生效，**这个项目其实也不受影响**——esbuild 0.25 的平台
二进制是通过 optionalDependency（`@esbuild/win32-x64`）分发的，不依赖 postinstall，
所以 `pnpm run build` 一直是好的。但 `pnpm install` 会以退出码 1 结束，
那会打断任何 CI 或脚本化的安装流程。所以还是要配对。

白名单应当保持"极短，且每一项都有理由"：每多批准一个包，
就多一分供应链风险。
"""

from __future__ import annotations

import json
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
PACKAGE = WEB / "package.json"
WORKSPACE = WEB / "pnpm-workspace.yaml"

#: 包名 -> 是否允许执行构建脚本
ALLOW_BUILDS = {
    "esbuild": True,
}


def main() -> None:
    # 1. package.json：清掉 pnpm 11+ 已经不读的 pnpm 字段。
    #    留着它会产生"看起来配了但实际无效"的假象，而那正是我踩过的坑。
    doc = json.loads(PACKAGE.read_text(encoding="utf-8"))
    removed = doc.pop("pnpm", None)
    PACKAGE.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"package.json: {'已移除失效的 pnpm 字段' if removed else '无需清理'}")

    # 2. pnpm-workspace.yaml：pnpm 11+ 读取设置的地方
    lines = [
        "# pnpm 设置。",
        "#",
        "# 为什么这个文件存在：pnpm 11 起不再读 package.json 里的 pnpm 字段，",
        "# 设置统一放这里；而且 v10 的 onlyBuiltDependencies 等四个键",
        "# 被合并成了单一的 allowBuilds 映射（见 https://pnpm.io/migration）。",
        "#",
        "# 默认不执行依赖的 postinstall 脚本（供应链安全）。这里只放行",
        "# 确实需要构建步骤的包：esbuild 用它安装平台二进制。",
        "# 白名单应当保持极短——每多批准一个包就多一分风险。",
        "",
        "packages: []",
        "",
        "allowBuilds:",
    ]
    for name, allowed in sorted(ALLOW_BUILDS.items()):
        lines.append(f"  {name}: {str(allowed).lower()}")
    WORKSPACE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {WORKSPACE}")
    for name, allowed in ALLOW_BUILDS.items():
        print(f"  {name}: {'允许' if allowed else '禁止'}")


if __name__ == "__main__":
    main()
