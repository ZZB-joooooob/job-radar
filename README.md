# job-radar

每天自动检查目标公司官网上新开的美国模拟 / 混合信号 / PMIC 芯片设计岗位。

- **什么时候跑**：每天 3:30 pm PST（夏令时 4:30 pm PDT），由 GitHub Actions 运行；在 Actions 页面点 *Run workflow* 可以手动多跑一次。
- **查哪些公司**：见 `companies.yaml`，可以随时增删。
- **怎么查**：只用各公司招聘系统公开的查询接口（Workday / Greenhouse / Lever），每个网站都先读 robots.txt，不允许的地方一律跳过；每次请求之间会停顿。
- **结果在哪**：`data/` 文件夹
  - `summary.md`：当天概况（先看这个）
  - `new_jobs.json`：今天第一次出现的岗位
  - `closed_jobs.json`：之前开着、今天下架的岗位
  - `open_jobs.json`：今天所有开着的匹配岗位
  - `health.json`：每家公司这次有没有查成功
- **之后**：每天 5:40 pm，Claude 读取这些结果，核对 JD、分档，写进 Notion 的「岗位雷达」表。
