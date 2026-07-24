# Device OAuth 可选 Mode + WebUI 切换设计

> 状态：设计稿（待实现）
> 目标：把 Device OAuth 作为现有 OAuth 的**可选 mode** 接入；WebUI 可切换「过盾方式」与「OAuth / 取 Token 方式」；默认 `auto` 行为不变。
> 给其他会话按本文清单直接实现。

---

## 1. 背景与问题

### 1.1 Protocol OAuth 的脆弱点

`login_with_protocol` 依赖 Next.js server action id：

| 常量 | 文件 | 现状 |
|---|---|---|
| `SUBMIT_OAUTH2_CONSENT_ACTION` | [`grok-build-auth/xconsole_client/oauth_protocol.py`](../grok-build-auth/xconsole_client/oauth_protocol.py) ~L51 | 旧硬编码 `4005315a1d7e426de592990bb54bb37471f39dd6d2` 已失效 |
| live 提取 | 同文件 `_submit_oauth2_consent` | 从 HTML 用 `createServerReference` + `submitOAuth2Consent` 兜底，但未登录 consent 页抓不到 chunk |

线上公共 JS 中 `getSession` 等 action id 持续变化（如 `00fc295c...` → `0091b719...`），说明 **consent action id 会随部署变更**，硬编码不可长期依赖。

### 1.2 用户 HAR 实际抓到的是 Device 批准端

`D:\Download\accounts.x.ai.har` **不是** `/oauth2/consent` + `next-action` 流，而是：

1. `POST https://auth.x.ai/oauth2/device/verify`
   body: `user_code=...`
2. `POST https://auth.x.ai/oauth2/device/approve`
   body: `user_code=...&action=allow&principal_type=User&principal_id=`
3. 到 `/oauth2/device/done`

无 `next-action` / `submitOAuth2Consent`。**Device 流不依赖 consent action id。**

### 1.3 仓库里其实已有完整 Device 实现

| 用途 | 位置 | 说明 |
|---|---|---|
| 注册后 SSO → token | [`scripts/sso_to_auth_json.py`](../scripts/sso_to_auth_json.py) | `request_device_code` / `verify` / `approve` / `poll_token` / `sso_to_token` |
| 账号页人工设备码登录 | [`grok2api/upstream/oidc_auth.py`](../grok2api/upstream/oidc_auth.py) | `start_device_authorization` + 后台 poll |
| 注册默认路径 | [`grok2api/upstream/grok_build_adapter.py`](../grok2api/upstream/grok_build_adapter.py) ~L3636+ | 注册成功后 **已经** 走 `sso_to_auth_json` device 流，**不是** `complete_build_oauth` |
| OAuth 编排 | [`grok-build-auth/xconsole_client/xai_oauth.py`](../grok-build-auth/xconsole_client/xai_oauth.py) | `complete_build_oauth` = protocol → playwright → (可选) browser |

关键事实：

- 当前注册入库主路径已经是 **SSO + Device approve**，与 protocol consent action 无关。
- `complete_build_oauth` / protocol / playwright 是另一条「authorization-code」能力，适合 CLI 导出、cliproxyapi、手动 OAuth 等场景。
- 账号页「设备码登录」是**人工** device（展示 user_code 等人批准）；注册用的是 **SSO cookie 自动 approve** 的 device。二者端点相同，会话语义不同。

---

## 2. 目标与非目标

### 2.1 目标

1. 后端 OAuth 提供统一 mode：
   ```text
   auto | protocol | device | playwright | browser
   ```
2. 默认 `auto`：**对外行为与现在一致**（见 §4.2）。
3. Device 作为**显式可选** mode；第一阶段 **不进入 auto 回退链**（除非后续验收通过再打开）。
4. WebUI 可切换：
   - **过盾方式**（已有）：Turnstile captcha
   - **OAuth / 取 Token 方式**（新增）：注册完成后如何拿 token
5. 其他组件复用同一结果模型 / 入库路径，不复制两套 token 落库逻辑。
6. 文档可执行：其他会话按清单改文件即可。

### 2.2 非目标（本阶段不做）

- 不要求随 xAI 部署修改 `SUBMIT_OAUTH2_CONSENT_ACTION`；优先 live 提取，并允许 WebUI override。
- 不把账号页人工 device UI 拆掉或替换为自动 SSO device。
- 不引入新的前端构建体系；沿用 `static/admin` + `static/dist/core.*.js` 现状（若有源码构建链则同步源码）。
- 不在 auto 里默认加 device，避免与现有注册主路径语义混淆、以及 device 限流叠加。

---

## 3. 概念区分（文案必须拆开）

| UI / 配置名 | 字段 | 含义 | 可选值 |
|---|---|---|---|
| **过盾方式** | `registration_config.captcha_provider` | 注册时解 Turnstile | `local` / `yescaptcha` |
| **OAuth / 取 Token 方式** | `registration_config.oauth_mode`（新增） | 注册拿到 SSO/会话后，如何换 access/refresh token | `auto` / `device` / `protocol` / `playwright` / `browser` |

用户说的「切换验证方式」在本设计中指 **OAuth / 取 Token 方式**；过盾方式保持现有控件，文案不要混叫「验证」。

---

## 4. 后端设计

### 4.1 Mode 语义

| mode | 行为 | 前置条件 | 适用 |
|---|---|---|---|
| `auto` | 见 §4.2 兼容策略 | 无 | 默认、批量注册 |
| `device` | SSO cookie → device/code → verify → approve → token poll | 有效 `sso`（或可登录得到 sso） | 注册后换 token（现状主路径）、无 consent action 时 |
| `protocol` | HTTP authorization-code + consent server action | cookies / CreateSession + captcha；consent action 可用 | CLI / 纯 HTTP OAuth |
| `playwright` | 浏览器自动填登录/点同意 | 本机 Chromium + 可选 session cookies | protocol 失败兜底 |
| `browser` | 系统浏览器人工点 | 有 GUI / 可交互 | 最后人工兜底 |

### 4.2 `auto` 兼容策略（必须行为不变）

**注册场景（`start_registration` 成功后的取 token）**

现状代码已经固定：

```text
create_account → SSO cookie → scripts.sso_to_auth_json.sso_to_token → import_auth_payload
```

因此注册路径的 `auto` **继续等于 `device`（SSO 自动批准）**，不要改成 protocol-first。
这样默认用户无感，且不依赖 consent action id。

**`complete_build_oauth` / CLI / 独立 OAuth 场景**

保持现有顺序：

```text
protocol → playwright → (interactive_fallback 时) browser
```

第一阶段 **device 不进这条 auto 链**。若调用方显式 `mode="device"`，才走 SSO device。

> 原因：注册与 CLI OAuth 的默认「成功路径」不同；混进 auto 会改失败面与限流特征。

### 4.3 新模块：`device_oauth.py`

路径建议：

```text
grok-build-auth/xconsole_client/device_oauth.py
```

职责：把现有 `scripts/sso_to_auth_json.py` 中的 device 能力抽成 **可被 xconsole_client 与 adapter 共用的库函数**，而不是让 `complete_build_oauth` 反向 import scripts。

建议 API：

```python
def request_device_code(
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str | list[str] | None = None,
    session: Any | None = None,
    proxy: str = "",
) -> dict: ...

def approve_device_login(
    *,
    user_code: str,
    sso_cookie: str | None = None,
    session: Any | None = None,
    proxy: str = "",
) -> None:
    """verify + approve；依赖 sso cookie 会话。"""

def poll_device_token(
    device_code: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    interval: float = 1.0,
    expires_in: int = 1800,
    timeout: float = 45.0,
    session: Any | None = None,
    proxy: str = "",
    immediate: bool = True,
) -> dict: ...

def login_with_device(
    *,
    sso_cookie: str,
    email: str = "",
    client_id: str = DEFAULT_CLIENT_ID,
    proxy: str = "",
    output_dir: Optional[str | Path] = None,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    cliproxyapi_disabled: bool = False,
) -> OAuthLoginResult:
    """SSO → device 全流程 → OAuthLoginResult。"""
```

实现要点：

1. **端点**（与现状 / HAR 一致）：
   - `POST {issuer}/oauth2/device/code`
   - `POST {issuer}/oauth2/device/verify` body `user_code`
   - `POST {issuer}/oauth2/device/approve` body
     `user_code&action=allow&principal_type=User&principal_id=`
   - `POST {issuer}/oauth2/token` grant_type device_code
2. 复用现有限流：`GROK2API_SSO_DEVICE_GAP_SEC` / `RETRIES` / `BACKOFF` / `POLL_TIMEOUT`。
3. 优先 `curl_cffi` impersonate chrome；无则 httpx/urllib。
4. 成功后走统一 finalize（见 §4.4），产出 `OAuthLoginResult`。
5. `scripts/sso_to_auth_json.py` 改为 **thin wrapper** 调用本模块，避免双实现漂移。

### 4.4 统一 finalize

现状：

- authorization-code 路径：`xai_oauth._finalize_oauth_code`（code → token → userinfo → save）
- device 路径：token 响应已是 token dict，只需 userinfo + save

建议新增：

```python
# xai_oauth.py
def _finalize_oauth_token(
    token: dict,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    proxy: str = "",
    output_dir: Optional[str | Path] = None,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    cliproxyapi_disabled: bool = False,
    redirect_uri: str = "",
) -> OAuthLoginResult:
    ...
```

`_finalize_oauth_code` 内部 exchange 后调用 `_finalize_oauth_token`。
`login_with_device` 直接调用 `_finalize_oauth_token`。

### 4.5 `complete_build_oauth` 扩展

文件：[`grok-build-auth/xconsole_client/xai_oauth.py`](../grok-build-auth/xconsole_client/xai_oauth.py)

签名变更（向后兼容）：

```python
def complete_build_oauth(
    email: str,
    password: str,
    *,
    mode: str = "auto",          # 新增
    # 保留旧参数
    protocol: bool = True,       # 废弃但兼容：mode=auto 时 protocol=False 跳过 protocol
    interactive_fallback: bool = False,
    session_cookies: Optional[Dict[str, str]] = None,
    auth_client: Any = None,
    sso_cookie: Optional[str] = None,  # 新增显式 SSO
    ...
) -> OAuthLoginResult:
```

调度伪代码：

```python
mode = (mode or "auto").strip().lower()
if mode not in {"auto", "protocol", "device", "playwright", "browser"}:
    raise ValueError(...)

sso = (sso_cookie or (session_cookies or {}).get("sso") or "").strip()

def run_device():
    if not sso:
        raise RuntimeError("device mode requires sso cookie")
    return login_with_device(sso_cookie=sso, email=email, ...)

def run_protocol():
    return login_with_protocol(...)

def run_playwright():
    return login_with_playwright(..., session_cookies=session_cookies)

def run_browser():
    return login_with_browser(...)

if mode == "device":
    return run_device()
if mode == "protocol":
    return run_protocol()
if mode == "playwright":
    return run_playwright()
if mode == "browser":
    return run_browser()

# mode == auto  （CLI / complete_build_oauth 语义）
errors = []
if protocol:
    try:
        return run_protocol()
    except Exception as e:
        errors.append(f"protocol: {e}")
try:
    return run_playwright()
except Exception as e:
    errors.append(f"playwright: {e}")
    if not interactive_fallback:
        raise RuntimeError("; ".join(errors)) from e
    return run_browser()
```

导出：更新 [`grok-build-auth/xconsole_client/__init__.py`](../grok-build-auth/xconsole_client/__init__.py)

```python
from .device_oauth import login_with_device, request_device_code, approve_device_login, poll_device_token
# __all__ 增加上述符号
```

### 4.6 注册路径如何接 `oauth_mode`

文件：[`grok2api/upstream/grok_build_adapter.py`](../grok2api/upstream/grok_build_adapter.py)

现状（~L3636+）固定：

```python
import scripts.sso_to_auth_json as sso_import
token = sso_import.sso_to_token(sso)
```

改为读取 session / reg_config 中的 `oauth_mode`：

```python
oauth_mode = (
    str(sess.get("oauth_mode") or reg_config.get("oauth_mode") or "auto")
    .strip()
    .lower()
)
if oauth_mode in {"", "auto"}:
    oauth_mode = "device"  # 注册 auto ≡ device

if oauth_mode == "device":
    token = sso_import.sso_to_token(sso)  # 内部已切到 device_oauth
elif oauth_mode in {"protocol", "playwright", "browser"}:
    from xconsole_client.xai_oauth import complete_build_oauth
    result = complete_build_oauth(
        email, password,
        mode=oauth_mode,
        session_cookies=session_cookies,
        sso_cookie=sso,
        proxy=proxy,
        yescaptcha_key=...,
        interactive_fallback=(oauth_mode == "browser"),
        ...
    )
    token = result.token
else:
    raise RuntimeError(f"unsupported oauth_mode: {oauth_mode}")
```

`start_registration` 增加参数：

```python
def start_registration(..., oauth_mode: str | None = None) -> dict:
    ...
    mode = (oauth_mode or cfg.get("oauth_mode") or "auto").strip().lower()
    if mode not in ALLOWED_OAUTH_MODES:
        mode = "auto"
    # 写入每个 session / batch reg_config
```

会话状态建议记录：

```python
sess["oauth"] = {
    "path": oauth_mode,  # device | protocol | playwright | browser
    "access_token": "...",
    ...
}
```

### 4.7 配置层

文件：[`grok2api/admin/settings_store.py`](../grok2api/admin/settings_store.py)

在 `_normalize_registration_config` 增加：

```python
ALLOWED_OAUTH_MODES = {"auto", "device", "protocol", "playwright", "browser"}

raw = _pick_str("oauth_mode", 32).lower()
if raw not in ALLOWED_OAUTH_MODES:
    raw = "auto"
cfg["oauth_mode"] = raw
```

`get_registration_config` / public payload 暴露 `oauth_mode`。
`apply_registration_config_to_runtime` 可选镜像：

```text
GROK2API_REG_OAUTH_MODE=auto|device|protocol|playwright|browser
```

`set_registration_config` 合并补丁时保留未知字段策略不变，仅规范化 `oauth_mode`。

### 4.8 与账号页 Device 登录的关系

| 能力 | API / UI | 实现 | 是否改动 |
|---|---|---|---|
| 人工设备码登录 | `#device-session` / `start_login(mode=device)` | `oidc_auth.start_device_authorization` | **保留**；第一阶段不强制重构 |
| 注册自动 device | 注册流水线 | `sso_to_auth_json` → 未来 `device_oauth` | **抽公共库后复用端点与限流** |
| OAuth mode=device | 注册配置 / complete_build_oauth | 同上 | 新增入口 |

长期可选项（非本阶段必做）：`oidc_auth` 的 code/token 请求也改调 `device_oauth.request_device_code` / `poll_device_token`，只保留「等人批准」会话状态机在 `oidc_auth`。

---

## 5. WebUI 设计

### 5.1 页面：`static/admin/accounts.html`

在「过盾方式」控件旁（`#reg-captcha-provider` 之后）新增：

```html
<div class="g2a-field">
  <label for="reg-oauth-mode">OAuth / 取 Token 方式</label>
  <select id="reg-oauth-mode">
    <option value="auto">自动（注册默认 Device）</option>
    <option value="device">Device（SSO 自动批准）</option>
    <option value="protocol">Protocol（HTTP + consent action）</option>
    <option value="playwright">Playwright 浏览器自动</option>
    <option value="browser">系统浏览器人工</option>
  </select>
  <div class="g2a-muted" style="margin-top:4px;font-size:12px">
    与「过盾方式」独立：过盾只解 Turnstile；此处决定注册拿到会话后如何换 access/refresh token。
    默认「自动」= Device，不依赖 Next.js consent action id。
  </div>
</div>
```

注意：

- **不要**把该下拉塞进「过盾」文案。
- `browser` 在 headless / Docker 中通常失败，可在 UI 用 muted 提示「需本机 GUI」。
- 账号页顶部「设备码登录」卡片保持不变。

### 5.2 前端逻辑：`static/dist/core.*.js`

以当前产物 [`static/dist/core.cfc11b8162.js`](../static/dist/core.cfc11b8162.js) 为准（若有未打包源码，改源码后重建；否则直接改当前被 index 引用的 core 文件，并同步其它 hash 副本或确认 HTML 引用）。

需要改的函数：

| 函数 | 改动 |
|---|---|
| `readRegConfig()` | 增加 `oauth_mode: ($("reg-oauth-mode")?.value \|\| "auto")` |
| `applyRegConfig(cfg)` | 回填 `#reg-oauth-mode` |
| `buildRegBody(config)` / 启动注册 body | 带上 `oauth_mode` |
| 保存/加载 config | 字段白名单加入 `oauth_mode` |

示例：

```javascript
// readRegConfig
oauth_mode: (function () {
  const v = $("reg-oauth-mode")
    ? String($("reg-oauth-mode").value || "auto").trim().toLowerCase()
    : "auto";
  const allowed = ["auto", "device", "protocol", "playwright", "browser"];
  return allowed.includes(v) ? v : "auto";
})(),

// applyRegConfig
if ($("reg-oauth-mode")) {
  const m = String(cfg.oauth_mode || "auto").trim().toLowerCase();
  const allowed = ["auto", "device", "protocol", "playwright", "browser"];
  $("reg-oauth-mode").value = allowed.includes(m) ? m : "auto";
}
```

### 5.3 API 传递链

```text
accounts.html #reg-oauth-mode
  → readRegConfig() / saveRegConfig()
  → PUT /accounts/register-email/config  { oauth_mode }
  → settings_store.set_registration_config
  → start 注册 POST /accounts/register-email  { oauth_mode, captcha_provider, ... }
  → scripts/registration_service → adapter.start_registration(oauth_mode=...)
  → session.reg_config.oauth_mode
  → 注册成功后按 mode 取 token → import_auth_payload
```

`captcha_provider` 链路保持不变。

### 5.4 配置 JSON 形状

```json
{
  "registration_config": {
    "captcha_provider": "local",
    "oauth_mode": "auto",
    "yescaptcha_key": "",
    "mail_provider": "moemail",
    "count": 1,
    "concurrency": 2
  }
}
```

缺省 / 非法值 → `oauth_mode = "auto"`。

---

## 6. Protocol consent action id（并行但独立）

`protocol` 模式支持在 WebUI 的「Protocol consent action id（可选）」中填写部署后的最新 id，保存到
`registration_config.oauth_consent_action_id`。留空时继续自动从 consent 页面提取，因此 xAI 更新 id
后通常无需修改或重新部署代码。

运行时解析优先级：

1. consent 页面中与 `submitOAuth2Consent` 对应的 live action id
2. WebUI / 环境变量显式填写的 override
3. 页面中的 generic `createServerReference` id
4. `SUBMIT_OAUTH2_CONSENT_ACTION` 兼容常量

环境变量兼容 `GROK2API_OAUTH_CONSENT_ACTION_ID`、`XAI_OAUTH_CONSENT_ACTION_ID` 和
`GROK2API_SUBMIT_OAUTH2_CONSENT_ACTION`。输入允许 `0x` 前缀或引号，保存时规范化为 40–44 位
小写十六进制字符串。该字段只作用于 `protocol`，Device 流不读取它。

若要人工确认新 id，可登录后打开真实 `/oauth2/consent?...`，抓 HAR 或用 Playwright 拦截
`next-action`，再直接填入 WebUI；不再要求更新代码常量。

临时分析产物（仅参考）：`.tmp/har_action_summary.json` 显示用户 HAR **无** consent next-action。

---

## 7. 分期实现

### Phase 0 — 文档与字段约定（本文）

- [x] 设计 MD
- [ ] 评审 mode 列表与注册 auto≡device

### Phase 1 — 库层 Device 统一（无 UI）

1. 新增 `xconsole_client/device_oauth.py`
2. 抽出 `_finalize_oauth_token`
3. `sso_to_auth_json` 改为调用 `device_oauth`（行为对齐单测/手工）
4. `complete_build_oauth(..., mode=)` 支持显式 mode
5. `__init__.py` 导出

验收：

- 现有注册（不改 UI）仍成功
- `login_with_device(sso)` 单测或脚本可换 token
- `mode=protocol|playwright` 与旧路径一致

### Phase 2 — 配置 + WebUI

1. `settings_store` 规范化 `oauth_mode`
2. `start_registration` / registration_service 透传
3. adapter 注册成功分支按 mode 取 token
4. `accounts.html` + `core.*.js` 控件与读写
5. 保存配置后刷新仍回显

验收：

- UI 切换 `device` / `protocol` / `playwright` 后，新注册 session 的 `oauth.path` 与选择一致
- `captcha_provider` 与 `oauth_mode` 互不覆盖
- 默认不选时等同今天行为

### Phase 3 — 硬化（可选）

1. `oidc_auth` 复用 `device_oauth` 的 code/token 请求
2. 验证 consent action id 自动提取，并保留 WebUI override 作为部署变更时的兜底
3. 评估是否允许 `auto` 在 CLI 场景 protocol 失败后尝试 device（需 SSO）
4. 指标：各 mode 成功率、device 429 次数

---

## 8. 文件改动清单

| 文件 | 动作 |
|---|---|
| `docs/device-oauth-optional-mode-design.md` | 本文 |
| `grok-build-auth/xconsole_client/device_oauth.py` | **新建** |
| `grok-build-auth/xconsole_client/xai_oauth.py` | `_finalize_oauth_token`；`complete_build_oauth(mode=)` |
| `grok-build-auth/xconsole_client/__init__.py` | 导出 device API |
| `scripts/sso_to_auth_json.py` | 委托 device_oauth，保留 CLI 入口 |
| `grok2api/admin/settings_store.py` | `oauth_mode` normalize/get/set/runtime |
| `grok2api/upstream/grok_build_adapter.py` | `start_registration` + 取 token 分支 |
| `scripts/registration_service.py` | 透传 `oauth_mode`（勿 pop 掉） |
| `static/admin/accounts.html` | `#reg-oauth-mode` |
| `static/dist/core.*.js` | read/apply/build body |
| `grok2api/upstream/oidc_auth.py` | Phase 3 可选复用 |

---

## 9. 执行清单（给实现会话）

按顺序勾选：

1. **确认当前 HTML 引用的 core hash**
   打开 `static/admin/accounts.html` / 布局脚本标签，只改实际加载的 `core.<hash>.js`，避免改错副本。
2. **新建 `device_oauth.py`**
   从 `scripts/sso_to_auth_json.py` 迁 `request_device_code` / verify+approve / `poll_token` / `sso_to_token` 核心，返回 dict 或 `OAuthLoginResult`。
3. **`xai_oauth._finalize_oauth_token`**
   code 路径与 device 路径共用 save/cliproxy/userinfo。
4. **`complete_build_oauth(mode=...)`**
   兼容 `protocol=bool`；显式 mode 短路。
5. **改 `sso_to_auth_json` 为 wrapper**
   跑一次 `sso_to_token` 手工验证（需真实 sso）。
6. **settings_store 加 `oauth_mode`**
   默认 `auto`；非法回落。
7. **adapter / registration_service 透传**
   session 记录 `oauth_mode` 与 `oauth.path`。
8. **注册取 token 分支**
   auto/device → 现网 device；protocol/playwright/browser → `complete_build_oauth`。
9. **WebUI**
   过盾与 OAuth 两个下拉；保存/加载/启动注册都带字段。
10. **验收**
    - 默认注册 1 个号成功
    - 显式 device 成功
    - 显式 protocol 在 consent 失效时失败信息可读
    - captcha 切换不受 oauth_mode 影响
    - 账号页人工设备码登录仍可用

---

## 10. 验收标准

### 功能

- [ ] 默认配置下注册入库路径与改前一致（SSO → device → token → postgres）
- [ ] WebUI 可选择并持久化 `oauth_mode`
- [x] WebUI 可填写、清空并持久化 `oauth_consent_action_id`
- [ ] `oauth_mode=device` 不依赖 consent action id
- [ ] `oauth_mode=playwright` 在装有 Chromium 的环境可用（或明确报错）
- [ ] 过盾方式 local/yescaptcha 行为不变

### 兼容

- [ ] 旧客户端不传 `oauth_mode` → 视为 `auto`
- [ ] `complete_build_oauth(..., protocol=False)` 仍跳过 protocol
- [ ] `start_login(mode="oauth")` 仍退化为人工 device（现网行为）

### 风险

| 风险 | 缓解 |
|---|---|
| Device 429 / slow_down | 沿用 gap/retries；批量注册 concurrency 提示 |
| protocol consent id 过期 | 默认 auto 不依赖；UI 标明 protocol 风险 |
| headless 下 browser/playwright | UI 提示；失败写入 session.error |
| 双实现漂移 | scripts 只做 wrapper |
| 前端多份 core hash | 只改被引用文件或统一构建 |

---

## 11. 测试建议

### 单元 / 轻量

- `oauth_mode` normalize：空、非法、大小写
- `complete_build_oauth` mode 分发（mock 子函数）
- device approve body 字段固定：`action=allow&principal_type=User&principal_id=`

### 集成（需代理 / 真实账号）

- 有效 SSO → `login_with_device` → 有 `access_token` + `refresh_token`
- 注册 1 账号 `oauth_mode=auto|device`
- 可选：`oauth_mode=protocol` 对照（consent 有效时）

### WebUI 手工

1. 打开账号页 → 协议注册配置
2. 过盾 = local，OAuth = device → 保存 → 刷新仍在
3. 启动 1 个注册 → session 日志含 device 路径
4. 切 OAuth = protocol → 新 session 路径变化
5. 设备码登录卡片仍可「开始设备码登录」

---

## 12. 给实现者的最小 diff 顺序

```text
1. device_oauth.py + finalize_token
2. complete_build_oauth(mode)
3. sso_to_auth_json wrapper
4. settings_store oauth_mode
5. adapter 取 token 分支 + start_registration 参数
6. registration_service 透传
7. accounts.html + core.js
8. 手工验收默认路径
```

---

## 13. 一句话决策摘要

- **注册默认取 token = Device（SSO 自动批准）**，不绑 consent action id。
- **Device 做成 xconsole_client 一等 mode**，供 `complete_build_oauth` 与注册复用。
- **WebUI 分开两个开关**：过盾 vs OAuth/取 Token。
- **auto 不改变现网默认**；protocol/playwright/browser 仅显式选择时走。
- **consent action id 可配置**；留空时 live 提取并回退兼容常量，无需为 id 变更改代码。

---

## 附录 A — 关键端点

```text
POST https://auth.x.ai/oauth2/device/code
  client_id, scope

POST https://auth.x.ai/oauth2/device/verify
  user_code

POST https://auth.x.ai/oauth2/device/approve
  user_code, action=allow, principal_type=User, principal_id=

POST https://auth.x.ai/oauth2/token
  grant_type=urn:ietf:params:oauth:grant-type:device_code
  device_code, client_id

# authorization-code（protocol / playwright）
GET  https://auth.x.ai/oauth2/auth?...
POST consent page next-action submitOAuth2Consent  # action id 易变
POST https://auth.x.ai/oauth2/token  (code + code_verifier)
```

## 附录 B — 现状代码锚点

| 锚点 | 位置 |
|---|---|
| consent action 常量 | `oauth_protocol.py` `SUBMIT_OAUTH2_CONSENT_ACTION` |
| consent action 配置 | `registration_config.oauth_consent_action_id` / `GROK2API_OAUTH_CONSENT_ACTION_ID` |
| OAuth 编排 | `xai_oauth.complete_build_oauth` |
| 注册后 device 转换 | `grok_build_adapter` → `sso_to_auth_json.sso_to_token` |
| 人工 device 登录 | `oidc_auth.start_device_authorization` |
| 过盾 UI | `accounts.html` `#reg-captcha-provider` |
| 注册配置读写 | `core.*.js` `readRegConfig` / `applyRegConfig` / `saveRegConfig` |
| 配置规范化 | `settings_store._normalize_registration_config` |

## 附录 C — 用户 HAR 结论

- 文件：`D:\Download\accounts.x.ai.har`
- 结论：device verify/approve/done；**无** consent next-action
- 因此「更新 consent id」需要**另一次**登录后的 consent 页抓包，不能从该 HAR 得到新 id
