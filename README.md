# 今日油价 · oil-price

现代化汽油价格页面 + 公开 API，部署在 Cloudflare Workers，代码由 GitHub 维护。

## 功能

- **基础**：31 省 92 / 95 / 98 / 0号柴油、搜索、排序、全国均价、调价倒计时
- **增强**：历史走势 + 调价日历、加油计算器、排行对比、城市级、多源校验
- **预测**：Brent / WTI + 汇率 + 10 工作日变化率，按发改委 50 元/吨门槛预测下次涨跌
- **对比与解读**：油 vs 电每公里成本（默认值标注为假设）、全球对比、每次调价一句话规则解读（标注为规则生成，非 LLM）
- **开发者**：OpenAPI + Playground + CSV 导出 + RSS / Webhook 说明、数据开放
- **体验**：PWA、暗色模式、移动端优先、车主账本（localStorage）、`/health` + 更新日志

**已去掉**：众包校验、企业 API。API 保持免 Key 公开。

## 数据来源与诚实性

- 唯一价格事实来源：[qiyoujiage.com](http://www.qiyoujiage.com/) 首页 31 省表。只存价格数字 + 调价时间，不抄文章。
- 种子数据：2026-10-15 实测（首个快照 `fetched_at: 2026-10-15T20:07:00+08:00`，验证重跑时为 20:09 同值），下次调整 10月15日24时，状态：搁浅（维持目前价格不变）。
- 历史从 2026-01-06 开始（20 个调价点）：2026-10-15 实测 + 之前 19 点按 2026 年各轮调价幅度反推（92/95 全国平均变动；98 按 95 估算，柴油按汽油×2290/2375 估算），只有一个点就只画一个点。
- 任何取不到 / 未验证的数据一律显示“暂无 / 待接入 / 收集中”，不编造、不补齐、不伪造历史。
- 页面页脚标注：数据来源 qiyoujiage.com、仅供参考、以发改委官方通知为准。

### 待接入 / 收集中（诚实清单）

| 功能 | 状态 | 原因 |
|---|---|---|
| 城市级价格（10 个非统一价省拆到市） | 已接入（124 城） | 源站城市页，异常标“待核对”不展示 |
| 预测（Brent/WTI + 汇率） | 已跑通（置信度低） | FRED Brent/WTI + ER-API 汇率；FRED 数据滞后数日、Brent/WTI 为官方篮子代理、系数 1.0 为估算假设；公式与来源见 `data/forecast.json` |
| 全球主要国家对比 | 已接入（欧盟 27 国 + 美国 + 中国） | 数据日期见各行 as_of |
| 发改委公告逐项对照（多源校验） | 待接入 | 对照数据未接入；不一致标红逻辑已实现 |

### 预测公式

10 个工作日一揽子原油价格变化率；按发改委规则，变化率对应调价金额不足 50 元/吨时不作调整（搁浅）。
上游：FRED Brent (`DCOILBRENTEU`) + WTI (`DCOILWTICO`) 免费 CSV + ER-API USD/CNY（免费，无 Key）。Stooq 于 2026-10-15 实测被 JS 验证墙拦截，已改用 FRED。任一上游取不到则 `status=no_data`。预测页展示每升变动（现价 → 预测价 → 每升变动 + 幅度 + 区间），数据来自 `/api/forecast` 的 `delta_per_liter`，不硬编码。

## 页面（History 真实多页，无 #）

| 路径 | 文件 | 内容 |
|---|---|---|
| `/` | `public/index.html` | 今日油价（Hero + 查询/排序/油号 + 价格表 + 分页 + 排行） |
| `/trend` | `public/trend.html` | 走势（历史走势图 + 调价日历 + 历史快照分页） |
| `/tools` | `public/tools.html` | 工具（加油计算器 + 油 vs 电 + 车主账本） |
| `/forecast` | `public/forecast.html` | 预测（国际油价预测 + 每升变动 + 全球 + 城市级） |
| `/api-docs` | `public/api-docs.html` | API 文档（Playground + OpenAPI + Webhook 说明） |

顶栏省份切换（`localStorage` key `oil-price-current-province`，默认上海）跨页持久；公共逻辑在 `public/shared.js`（主题、省份、分页、空状态）。样式用 DaisyUI v5（Tailwind v4 `@plugin "daisyui"`，主题 light/dark，与 `.dark` class 同步）。

## API

免 Key，CORS 全开。完整定义见 `openapi.yaml` / `/openapi.yaml`。

```
GET /api/prices[?province=上海&oil=95]
GET /api/history
GET /api/trend?province=上海&oil=95
GET /api/meta
GET /api/cities[?province=]
GET /api/forecast
GET /api/global
GET /api/export.csv
GET /health
GET /rss.xml
GET /openapi.yaml
```

Webhook：轮询 `/api/meta` 的 `last_change_at`（价格变化时才更新）或订阅 `/rss.xml`。`last_checked_at` 为上次抓取检查时间。暂不提供推送服务。

## 本地开发

```bash
npm install
npm run sync-data   # data/ -> public/data/
npm run build:css   # Tailwind CLI：src/tailwind.css -> public/tailwind.css（改样式后必跑）
npm run dev         # wrangler dev，默认 http://localhost:8787
curl localhost:8787/api/prices | head -c 500
curl localhost:8787/health
```

## 数据更新

GitHub Actions（`.github/workflows/update-prices.yml`）：每天 02:00 UTC + 可手动触发，跑 `scripts/scrape.py` 抓首页解析 → 写 `data/prices.json`、`data/history.jsonl`（有变动才追加）、`data/meta.json`，提交回仓库。`scrape.py` 带 UA 标识、失败重试 2 次、解析失败则不写脏数据直接失败退出。不要高频爬。

## 部署（已上线）

- 页面：https://oil.fansys.top/（自定义域名；https://oil-price.fanzy.workers.dev 同服务）
- 仓库：https://github.com/fanzbus/oil-price
- 部署：`npx wrangler deploy`（Cloudflare Workers，静态 + API 同服务）。改代码后重新 deploy 即可；数据更新（价格每日 02:00 UTC / 预测每小时）由 GitHub Actions 提交后自动 deploy（需仓库配置 CLOUDFLARE_API_TOKEN secret，未配置时部署步骤自动跳过、仍可手动 deploy）。

## 许可证

价格数据为事实数据；代码 MIT（建议建仓时加 LICENSE）。
