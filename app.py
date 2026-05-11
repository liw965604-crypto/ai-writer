import streamlit as st
import sqlite3
import os
import secrets
import string
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI

# 加载 .env
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

st.set_page_config(
    page_title="AI 文章生成器",
    page_icon="✍️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============================================================
# 平台风格 Prompt 模板
# ============================================================
PLATFORM_PROMPTS = {
    "公众号": {
        "style": "深度长文风格",
        "system_prompt": """你是一位资深公众号写手，擅长撰写面向中产阶级的深度文章。
写作要求：
- 开头用故事或痛点切入，3秒抓住读者注意力
- 正文段落分明，每段200字左右，用小标题分隔
- 观点鲜明，有自己的独特见解，不要人云亦云
- 结尾要有总结和金句，引导读者转发
- 语言精炼但不失温度，像在和读者面对面聊天
- 适当使用加粗、引用等格式增强可读性""",
    },
    "小红书": {
        "style": "种草分享风格",
        "system_prompt": """你是一位小红书爆款文案写手。
写作要求：
- 每段1-2句话就换行，大量留白
- 大量使用emoji（每段至少2-3个）
- 口语化表达，像闺蜜在安利东西
- 用"姐妹们"、"绝绝子"、"谁懂啊"等小红书高频词
- 用#话题标签收尾（至少5个）
- 用数字列表排版（一、二、三...）
- 语气真诚、有感染力，避免营销腔""",
    },
    "知乎": {
        "style": "专业严谨风格",
        "system_prompt": """你是一位知乎高赞答主，擅长撰写专业深度的回答。
写作要求：
- 开头用一句话总结核心观点（先放结论）
- 分点论述，逻辑清晰，有理有据
- 引用数据和研究时要标注来源思路
- 语言客观理性，避免情绪化表达
- 适当使用专业术语，但要解释清楚
- 结尾可以抛出延伸思考的问题
- 善用引用块、列表等排版提升阅读体验""",
    },
    "今日头条": {
        "style": "通俗爆款风格",
        "system_prompt": """你是一位今日头条爆款文章写手。
写作要求：
- 标题要有冲击力，用数字、对比、悬念
- 开头30字内必须抓人眼球
- 语言通俗易懂，小学文化也能看懂
- 善用短句，每句话不超过30字
- 情绪饱满，立场鲜明，引发读者共鸣
- 适当制造争议点，刺激评论欲望
- 多用"你知道吗"、"惊人"、"万万没想到"等爆款词""",
    },
}

# 管理员主密钥 + 默认激活码（优先从 st.secrets 读取，其次环境变量，最后默认值）
def _cfg(key, default):
    try:
        return st.secrets.get(key, os.getenv(key, default))
    except Exception:
        return os.getenv(key, default)

ADMIN_MASTER_KEY = _cfg("ADMIN_MASTER_KEY", "admin-2024-ai-writer")
DEFAULT_ADMIN_CODE = _cfg("DEFAULT_ADMIN_CODE", "")  # 空则自动生成随机码

# ============================================================
# 数据库
# ============================================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            topic TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activation_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 100,
            used_count INTEGER NOT NULL DEFAULT 0,
            plan_name TEXT NOT NULL DEFAULT '基础版',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            expires_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # 如果没有激活码，生成一个默认管理员激活码
    count = conn.execute("SELECT COUNT(*) FROM activation_codes").fetchone()[0]
    if count == 0:
        if DEFAULT_ADMIN_CODE:
            default_code = DEFAULT_ADMIN_CODE
        else:
            default_code = "ADMIN-" + secrets.token_hex(4).upper()
        conn.execute(
            "INSERT INTO activation_codes (code, max_uses, plan_name, created_at) VALUES (?, ?, ?, ?)",
            (default_code, 9999, "管理员默认码", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        print(f"[初始化] 默认激活码: {default_code}")
    conn.commit()
    conn.close()


def get_conn():
    return sqlite3.connect(DB_PATH)


# ---------- 激活码操作 ----------
def verify_code(code):
    """验证激活码，返回 (valid, plan_name, remaining, message)"""
    conn = get_conn()
    row = conn.execute(
        "SELECT code, max_uses, used_count, plan_name, is_active, expires_at FROM activation_codes WHERE code = ?",
        (code.strip().upper(),),
    ).fetchone()
    conn.close()

    if not row:
        return False, "", 0, "激活码无效，请检查后重试"

    code_val, max_uses, used, plan, active, expires = row

    if not active:
        return False, "", 0, "该激活码已被停用"

    if expires:
        expires_dt = datetime.strptime(expires, "%Y-%m-%d")
        if datetime.now() > expires_dt:
            return False, "", 0, f"激活码已过期（{expires}）"

    if used >= max_uses:
        return False, "", 0, f"该激活码次数已用完（{used}/{max_uses}）"

    remaining = max_uses - used
    return True, plan, remaining, "激活成功"


def use_code(code):
    """消耗一次激活码次数"""
    conn = get_conn()
    conn.execute(
        "UPDATE activation_codes SET used_count = used_count + 1 WHERE code = ?",
        (code,),
    )
    conn.execute(
        "INSERT INTO usage_log (code, action, created_at) VALUES (?, ?, ?)",
        (code, "generate", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def generate_code(plan_name, max_uses, expires_days):
    """生成一个新的激活码"""
    chars = string.ascii_uppercase + string.digits
    code = secrets.token_hex(4).upper()  # 8位 hex = 如 "A3F8B2C1"
    expires_at = None
    if expires_days > 0:
        expires_at = (datetime.now() + timedelta(days=expires_days)).strftime("%Y-%m-%d")

    conn = get_conn()
    conn.execute(
        "INSERT INTO activation_codes (code, max_uses, plan_name, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (code, max_uses, plan_name, expires_at, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()
    return code


def list_codes():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, code, max_uses, used_count, plan_name, is_active, created_at, expires_at FROM activation_codes ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return rows


def toggle_code_status(code_id, active):
    conn = get_conn()
    conn.execute("UPDATE activation_codes SET is_active = ? WHERE id = ?", (active, code_id))
    conn.commit()
    conn.close()


def delete_code(code_id):
    conn = get_conn()
    conn.execute("DELETE FROM activation_codes WHERE id = ?", (code_id,))
    conn.commit()
    conn.close()


# ---------- 文章操作 ----------
def save_article(platform, topic, title, content):
    conn = get_conn()
    conn.execute(
        "INSERT INTO articles (platform, topic, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (platform, topic, title, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def load_articles(limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, platform, topic, title, created_at FROM articles ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def load_article_by_id(article_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    conn.close()
    return row


def delete_article(article_id):
    conn = get_conn()
    conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    conn.commit()
    conn.close()


def get_article_count():
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    return count


# ============================================================
# AI 生成核心
# ============================================================
def build_messages(platform, topic, word_count):
    cfg = PLATFORM_PROMPTS[platform]
    return [
        {"role": "system", "content": cfg["system_prompt"]},
        {"role": "user", "content": f"""请根据以下主题写一篇文章：

【主题】{topic}
【平台】{platform}
【字数】约{word_count}字

请按以下格式输出：

## 标题
（生成一个吸引人的标题）

## 正文
（文章正文内容）

注意：严格按照 {cfg['style']} 来写，要符合{platform}平台的调性。"""},
    ]


def generate_article(api_key, base_url, model, platform, topic, word_count, temperature):
    client = OpenAI(api_key=api_key, base_url=base_url)
    messages = build_messages(platform, topic, word_count)
    response = client.chat.completions.create(
        model=model, messages=messages, temperature=temperature, max_tokens=word_count * 4,
    )
    raw_text = response.choices[0].message.content.strip()
    return parse_article(raw_text)


def parse_article(raw_text):
    title = ""
    for line in raw_text.split("\n"):
        s = line.strip()
        if s.startswith("## 标题") or s.startswith("##标题"):
            after = s.replace("## 标题", "").replace("##标题", "").strip()
            if after.startswith("："):
                after = after[1:].strip()
            if after:
                title = after
            continue
        if s.startswith("# ") and not title:
            title = s[2:].strip()
            continue

    if not title:
        for line in raw_text.split("\n"):
            s = line.strip()
            if s and not s.startswith("#"):
                title = s[:50]
                break

    content_lines = []
    for line in raw_text.split("\n"):
        s = line.strip()
        if s.startswith("## 标题") or s.startswith("##标题"):
            continue
        content_lines.append(line)

    return title, "\n".join(content_lines).strip()


def generate_batch(api_key, base_url, model, platform, topics, word_count, temperature, code):
    results = []
    progress_bar = st.progress(0)
    status_text = st.empty()

    for i, topic in enumerate(topics):
        status_text.text(f"正在生成第 {i+1}/{len(topics)} 篇：{topic}")
        try:
            title, content = generate_article(api_key, base_url, model, platform, topic.strip(), word_count, temperature)
            save_article(platform, topic.strip(), title, content)
            use_code(code)
            results.append({"topic": topic.strip(), "title": title, "content": content, "error": None})
        except Exception as e:
            results.append({"topic": topic.strip(), "title": "", "content": "", "error": str(e)})
        progress_bar.progress((i + 1) / len(topics))

    status_text.empty()
    progress_bar.empty()
    return results


# ============================================================
# 激活码登录页面
# ============================================================
def render_login():
    st.title("🔐 激活码验证")
    st.caption("请输入购买获取的激活码来使用 AI 文章生成器")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        code_input = st.text_input(
            "激活码",
            placeholder="如：A3F8B2C1",
            help="8位字母数字组合",
        ).strip().upper()

        login_btn = st.button("✅ 激活使用", type="primary", use_container_width=True)

        if login_btn and code_input:
            valid, plan, remaining, msg = verify_code(code_input)
            if valid:
                st.session_state["activated"] = True
                st.session_state["active_code"] = code_input
                st.session_state["plan_name"] = plan
                st.session_state["remaining"] = remaining
                st.success(f"激活成功！套餐：{plan}，剩余次数：{remaining}")
                st.rerun()
            else:
                st.error(msg)

        st.divider()
        st.caption("💡 还没有激活码？联系卖家购买：")
        st.caption("📧 将你的联系方式放在这里")
        st.caption("💰 套餐价格在这里展示")


# ============================================================
# 管理员面板
# ============================================================
def render_admin():
    st.title("🔧 管理员面板")

    tab1, tab2 = st.tabs(["📋 激活码管理", "➕ 生成新码"])

    with tab1:
        codes = list_codes()
        if codes:
            st.caption(f"共 {len(codes)} 个激活码")
            for c in codes:
                cid, code, max_u, used, plan, active, created, expires = c
                status = "🟢 正常" if active else "🔴 停用"
                exp_str = expires if expires else "永不过期"

                with st.expander(f"{status} [{plan}] {code} — {used}/{max_u}（到期：{exp_str}）"):
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        st.metric("总次数", max_u)
                    with c2:
                        st.metric("已用", used)
                    with c3:
                        st.metric("剩余", max_u - used)

                    col1, col2 = st.columns(2)
                    with col1:
                        if active:
                            if st.button("🔴 停用此码", key=f"deact_{cid}"):
                                toggle_code_status(cid, 0)
                                st.rerun()
                        else:
                            if st.button("🟢 启用此码", key=f"act_{cid}"):
                                toggle_code_status(cid, 1)
                                st.rerun()
                    with col2:
                        if st.button("🗑️ 删除", key=f"delcode_{cid}"):
                            delete_code(cid)
                            st.rerun()
        else:
            st.info("还没有激活码")

    with tab2:
        st.subheader("➕ 生成新的激活码")

        col1, col2, col3 = st.columns(3)
        with col1:
            plan_options = st.selectbox("套餐类型", ["基础版 (100次)", "标准版 (300次)", "高级版 (500次)", "旗舰版 (1000次)", "自定义"])
        with col2:
            if plan_options == "自定义":
                max_uses = st.number_input("使用次数", min_value=1, max_value=99999, value=100)
            else:
                uses_map = {"基础版 (100次)": 100, "标准版 (300次)": 300, "高级版 (500次)": 500, "旗舰版 (1000次)": 1000}
                max_uses = uses_map[plan_options]
                plan_name = plan_options.split(" (")[0]
                st.metric("次数", max_uses)
        with col3:
            expires_days = st.number_input("有效期（天，0=永久）", min_value=0, max_value=3650, value=365)

        if plan_options == "自定义":
            plan_name = st.text_input("套餐名称", value="自定义套餐")

        generate_count = st.number_input("生成数量", min_value=1, max_value=100, value=1)

        if st.button("🎫 生成激活码", type="primary", use_container_width=True):
            new_codes = []
            for _ in range(generate_count):
                new_codes.append(generate_code(plan_name, max_uses, expires_days))

            st.success(f"成功生成 {len(new_codes)} 个激活码：")
            for c in new_codes:
                st.code(c, language=None)

            st.info("复制以上激活码，发送给付费用户即可")


# ============================================================
# 主界面（激活后的正常使用界面）
# ============================================================
def render_sidebar():
    with st.sidebar:
        # 激活信息
        st.title("📊 账号状态")
        st.metric("套餐", st.session_state.get("plan_name", "未知"))
        remaining = st.session_state.get("remaining", 0)
        st.metric("剩余次数", remaining)
        if remaining < 10:
            st.warning("次数即将用完，请联系续费")

        st.divider()
        st.title("⚙️ API 配置")

        preset = st.selectbox(
            "API 预设",
            ["DeepSeek（推荐，国内可用）", "OpenAI", "自定义"],
        )

        defaults = {
            "DeepSeek（推荐，国内可用）": ("https://api.deepseek.com", "deepseek-chat"),
            "OpenAI": ("https://api.openai.com/v1", "gpt-4o"),
            "自定义": ("https://api.deepseek.com", "deepseek-chat"),
        }
        default_url, default_model = defaults[preset]

        api_key = st.text_input(
            "API Key",
            type="password",
            value=os.getenv("DEEPSEEK_API_KEY", ""),
            placeholder="sk-xxxxxxxx",
        )
        base_url = st.text_input("API Base URL", value=os.getenv("DEEPSEEK_BASE_URL", default_url))
        model = st.text_input("模型名称", value=default_model)

        st.divider()
        st.title("🎛️ 生成参数")

        temperature = st.slider(
            "创意度", 0.0, 2.0, 0.8, 0.1,
            help="越高越有创意，越低越保守",
        )

        st.divider()

        # 管理员入口
        with st.expander("🔧 管理员入口"):
            master_key = st.text_input("管理员密钥", type="password", key="master_key_input")
            if st.button("进入管理面板"):
                if master_key == ADMIN_MASTER_KEY:
                    st.session_state["show_admin"] = True
                    st.rerun()
                else:
                    st.error("密钥错误")

        # 退出登录
        if st.button("🚪 退出登录", use_container_width=True):
            for k in ["activated", "active_code", "plan_name", "remaining", "show_admin"]:
                st.session_state.pop(k, None)
            st.rerun()

        return api_key, base_url, model, temperature


def render_main(api_key, base_url, model, temperature):
    st.title("✍️ AI 文章生成器")
    st.caption("输入主题，AI 帮你写出符合平台调性的爆款文章")

    tab1, tab2, tab3 = st.tabs(["📝 单篇生成", "📚 批量生成", "📋 历史记录"])

    code = st.session_state.get("active_code", "")

    with tab1:
        col1, col2, _ = st.columns(3)
        with col1:
            platform = st.selectbox("目标平台", list(PLATFORM_PROMPTS.keys()))
        with col2:
            word_count = st.selectbox("文章字数", [500, 800, 1000, 1500, 2000], index=1)

        topic = st.text_area(
            "文章主题 / 关键词",
            placeholder="例如：35岁职场危机、减肥的10个误区、2024年投资理财建议...",
            height=80,
        )

        gen_btn = st.button("🚀 生成文章", type="primary", use_container_width=True, disabled=not api_key)

        if gen_btn:
            if not topic.strip():
                st.error("请输入文章主题")
            else:
                # 再次验证激活码
                valid, _, remaining, msg = verify_code(code)
                if not valid:
                    st.error(msg)
                    if "用完" in msg or "过期" in msg or "停用" in msg:
                        st.session_state.pop("activated", None)
                        st.rerun()
                else:
                    with st.spinner(f"AI 正在为你撰写{platform}风格的文章..."):
                        try:
                            title, content = generate_article(
                                api_key, base_url, model, platform, topic.strip(), word_count, temperature
                            )
                            save_article(platform, topic.strip(), title, content)
                            use_code(code)
                            st.session_state["remaining"] = remaining - 1

                            st.divider()
                            st.subheader("📌 生成结果")
                            st.markdown(f"### {title}")
                            st.markdown(content)

                            st.divider()
                            c1, c2, c3 = st.columns(3)
                            with c1:
                                full_text = f"{title}\n\n{content}"
                                st.download_button(
                                    "📥 下载 Markdown",
                                    data=full_text,
                                    file_name=f"{title[:20]}.md",
                                    mime="text/markdown",
                                    use_container_width=True,
                                )
                            with c2:
                                st.info(f"剩余次数：{remaining - 1}")
                            with c3:
                                st.success("已保存到历史记录")

                        except Exception as e:
                            st.error(f"生成失败：{e}")

    with tab2:
        st.subheader("📚 批量生成多篇文章")
        st.caption("每行一个主题，AI 会依次为你生成（每篇消耗 1 次额度）")

        col1, col2 = st.columns(2)
        with col1:
            batch_platform = st.selectbox("目标平台", list(PLATFORM_PROMPTS.keys()), key="batch_platform")
        with col2:
            batch_word_count = st.selectbox("文章字数", [500, 800, 1000, 1500, 2000], index=1, key="batch_words")

        batch_topics = st.text_area(
            "输入多个主题（每行一个）",
            placeholder="35岁职场危机\n减肥的10个误区\n2024年投资理财建议",
            height=150,
        )

        batch_btn = st.button("🚀 开始批量生成", type="primary", use_container_width=True, disabled=not api_key)

        if batch_btn:
            topics_list = [t for t in batch_topics.strip().split("\n") if t.strip()]
            if not topics_list:
                st.error("请至少输入一个主题")
            else:
                valid, _, _, msg = verify_code(code)
                if not valid:
                    st.error(msg)
                elif len(topics_list) > remaining:
                    st.error(f"剩余次数不足！需要 {len(topics_list)} 次，剩余 {remaining} 次")
                else:
                    results = generate_batch(
                        api_key, base_url, model, batch_platform,
                        topics_list, batch_word_count, temperature, code,
                    )
                    remaining_after = verify_code(code)[2]
                    st.session_state["remaining"] = remaining_after

                    st.divider()
                    st.subheader(f"生成完成（{sum(1 for r in results if not r['error'])}/{len(results)} 成功）")

                    for i, r in enumerate(results):
                        with st.expander(f"第{i+1}篇：{r['topic']} — {r.get('title', '失败')}", expanded=i == 0):
                            if r["error"]:
                                st.error(f"失败：{r['error']}")
                            else:
                                st.markdown(f"### {r['title']}")
                                st.markdown(r["content"])

    with tab3:
        st.subheader("📋 历史生成记录")
        articles = load_articles()

        if not articles:
            st.info("还没有生成过文章，快去试试吧！")
        else:
            st.caption(f"共 {get_article_count()} 篇")
            for art in articles:
                art_id, platform, topic, title, created_at = art
                with st.expander(f"[{platform}] {title} — {created_at}"):
                    full = load_article_by_id(art_id)
                    if full:
                        st.markdown(f"**主题：** {topic}")
                        st.markdown(full[4])
                    c1, c2 = st.columns(2)
                    with c1:
                        full_text = f"{title}\n\n{full[4]}"
                        st.download_button("📥 下载", data=full_text, file_name=f"{title[:20]}.md",
                                           mime="text/markdown", key=f"dl_{art_id}")
                    with c2:
                        if st.button("🗑️ 删除", key=f"del_{art_id}"):
                            delete_article(art_id)
                            st.rerun()


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    init_db()

    # 初始化 session state
    for key, default in [("activated", False), ("active_code", ""), ("plan_name", ""),
                          ("remaining", 0), ("show_admin", False)]:
        if key not in st.session_state:
            st.session_state[key] = default

    # 管理面板
    if st.session_state.get("show_admin"):
        render_admin()
        if st.button("⬅️ 返回主界面", use_container_width=True):
            st.session_state["show_admin"] = False
            st.rerun()
    # 未激活 → 登录页
    elif not st.session_state.get("activated"):
        render_login()
    # 已激活 → 正常使用
    else:
        api_key, base_url, model, temperature = render_sidebar()
        render_main(api_key, base_url, model, temperature)
