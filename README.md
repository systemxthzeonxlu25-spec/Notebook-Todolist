# Notebook-Todolist
笔记、待办、复习卡合为一体的学生学习笔记应用 | Flask + SQLite + 原生 JS

核心理念：**笔记、待办、复习卡是同一份内容的不同视图。**

## 本地运行

需要 Python 3.12。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python app.py
```

默认监听本机 5000 端口。数据库和上传目录在被 Git 忽略的 `instance/` 中。
`SECRET_KEY`、`DATABASE_PATH`、`TZ` 等配置从环境变量读取；`.env` 只用于本地配置，禁止提交。

## 当前功能

- 一周课表、按当前课程自动分类笔记，也可手动调整。
- Markdown 编辑和安全渲染，课程筛选与全文搜索。
- `- [ ]` 自动提取待办，默认在该课程下一节课截止。
- `==重点==` 自动生成挖空卡，按 1/3/7/15 天复习，遗忘后重置。
- 今日页汇总到期和过期待办、复习卡；移动端和深色模式。

## 自测

```bash
python -m pytest -q
```

## Known Issues

- 当前阶段尚未加入账号登录，仅供本机开发，后续阶段加入后再部署公网。
- 待办完成状态暂不反向修改笔记中的 Markdown 复选框。
