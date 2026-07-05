# Battery Paper Agent No API

这是一个**无 OpenAI/ChatGPT API 费用**的电池管理论文日报智能体模板。

它每天自动检索电池管理相关论文，基于关键词、期刊白名单、日期和摘要相关性进行规则打分，生成中文 HTML 日报，并支持：

- GitHub Actions 定时运行；
- arXiv / OpenAlex / Crossref 免费检索；
- HTML 邮件推送；
- GitHub Pages 自动归档；
- 不调用 OpenAI API，不消耗 ChatGPT/API token。

## 1. 工作流

```text
GitHub Actions 定时触发
   ↓
arXiv / OpenAlex / Crossref 检索论文元数据
   ↓
标题 + 摘要 + 期刊 + 日期 + 排除词规则打分
   ↓
A/B/C 分级
   ↓
生成 outputs/YYYY-MM-DD.html
   ↓
发送 HTML 邮件
   ↓
部署到 GitHub Pages 归档
```

## 2. 适合检索的方向

默认关注：

```text
BMS
SOC / SOH / RUL
故障诊断
安全预警
热失控预警
pack inconsistency
EV fleet / field data
储能系统健康管理
数字孪生
physics-informed learning
cloud BMS
```

默认排除：

```text
纯材料制备
电解液添加剂
催化
纯DFT材料计算
锂金属/钠金属材料方向
与BMS无关的回收工艺
```

你可以在 `config.yaml` 中自由调整。

## 3. 部署步骤

### Step 1：新建 GitHub 仓库

建议仓库名：

```text
battery-paper-agent-no-api
```

把本项目所有文件上传到仓库根目录。

### Step 2：开启 GitHub Pages

进入仓库：

```text
Settings → Pages → Build and deployment → Source → GitHub Actions
```

### Step 3：添加 Secrets

进入：

```text
Settings → Secrets and variables → Actions → New repository secret
```

至少建议添加：

| Secret | 作用 | 是否必须 |
|---|---|---|
| `CONTACT_EMAIL` | 给 OpenAlex/Crossref 的联系邮箱 | 建议 |
| `SMTP_HOST` | SMTP服务器，例如 `smtp.qq.com` | 邮件推送必须 |
| `SMTP_PORT` | SMTP端口，常用 `465` | 邮件推送必须 |
| `SMTP_USER` | 发件邮箱账号 | 邮件推送必须 |
| `SMTP_PASS` | SMTP授权码，不是邮箱登录密码 | 邮件推送必须 |
| `EMAIL_FROM` | 发件邮箱 | 邮件推送必须 |
| `EMAIL_TO` | 收件邮箱，多个用英文逗号分隔 | 邮件推送必须 |

如果不配置 SMTP，脚本仍会生成 HTML 并部署 GitHub Pages，只是跳过邮件发送。

### Step 4：手动测试

进入：

```text
Actions → Daily BMS Paper Agent No API → Run workflow
```

成功后会看到：

```text
outputs/YYYY-MM-DD.html
outputs/YYYY-MM-DD.json
outputs/index.html
```

网页归档地址一般是：

```text
https://你的GitHub用户名.github.io/仓库名/
```

### Step 5：自动运行时间

默认 GitHub Actions：

```yaml
cron: "30 0 * * *"
```

GitHub Actions 的 cron 使用 UTC 时间，因此对应北京时间/新加坡时间每天 **08:30**。

## 4. 本地运行

```bash
pip install -r requirements.txt
python daily_paper_agent.py
```

如需测试邮件，可设置环境变量：

```bash
export CONTACT_EMAIL="your_email@example.com"
export SMTP_HOST="smtp.qq.com"
export SMTP_PORT="465"
export SMTP_USER="your_email@qq.com"
export SMTP_PASS="your_smtp_authorization_code"
export EMAIL_FROM="your_email@qq.com"
export EMAIL_TO="receiver@example.com"
python daily_paper_agent.py
```

Windows PowerShell 示例：

```powershell
$env:CONTACT_EMAIL="your_email@example.com"
$env:SMTP_HOST="smtp.qq.com"
$env:SMTP_PORT="465"
$env:SMTP_USER="your_email@qq.com"
$env:SMTP_PASS="your_smtp_authorization_code"
$env:EMAIL_FROM="your_email@qq.com"
$env:EMAIL_TO="receiver@example.com"
python daily_paper_agent.py
```

## 5. 调整筛选规则

主要修改 `config.yaml`。

### 增加关注方向

```yaml
queries:
  include:
    - "battery management system"
    - "state of health battery"
    - "battery pack inconsistency"
    - "cloud battery management"
```

### 增加排除方向

```yaml
queries:
  exclude:
    - "electrolyte additive"
    - "cathode synthesis"
```

### 修改输出数量

```yaml
run:
  max_final_papers: 12
  include_c_level: false
```

### 增加重点期刊

```yaml
journal_whitelist:
  - "Nature Energy"
  - "Joule"
  - "Energy Storage Materials"
  - "Journal of Power Sources"
```

## 6. A/B/C 分级逻辑

规则分数由以下因素组成：

- BMS关键词命中；
- 标题中是否出现核心方向词；
- 是否属于重点期刊白名单；
- 是否为最近2天或最近7天上线；
- 是否命中纯材料/电解液/催化等排除词。

默认：

```text
A：score >= 80
B：score >= 48
C：score < 48
```

日报默认只输出 A/B 级，避免噪声过多。

## 7. 无API版的优缺点

优点：

- 不消耗 OpenAI / ChatGPT API token；
- 可以每天稳定自动运行；
- 结果可归档、可邮件推送；
- 便于课题组内部共享。

缺点：

- 中文总结是模板化生成，不如 LLM 凝练自然；
- 无法深度判断创新性和方法细节；
- 对摘要缺失的论文判断能力较弱。

建议先运行 3–5 天，根据误报/漏报结果调整 `config.yaml`。后续也可以升级为“工作日无API版 + 周五API深度综述版”。
