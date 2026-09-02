# -*- coding: utf-8 -*-
"""
云阳县民德小学课后服务报名系统
- 前端：各班级账号登录，班主任为班级学生报名
  - 精品社团：全校共用（不分年级）
  - 普通社团：每个年级各有
  - 一个学生只能报一个社团；满30人截止；不足15人可先报，由管理员决定
- 后台：管理员创建社团/班级账号、审核报名、查看统计、导出CSV
"""
import os
import sqlite3
import csv
import io
import traceback
from datetime import datetime
from flask import (Flask, request, redirect, url_for, render_template,
                   session, flash, jsonify, make_response, g)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'after-school-secret-key-change-me')

# 数据目录：Railway 挂载卷 /data，本地开发用本地目录
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(__file__), 'data'))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'afterschool.db')

GRADES = ['一年级', '二年级', '三年级', '四年级', '五年级', '六年级']

# 社团分类（普通社团按 年级×分类 设立）
CATEGORIES = ['体育', '艺术', '文化', '科技', '益智游戏']

# 上课时间规则：年级 -> 星期（一、四周二；二、五周三；三、六周四）
GRADE_SCHEDULE = {
    '一年级': '星期二', '四年级': '星期二',
    '二年级': '星期三', '五年级': '星期三',
    '三年级': '星期四', '六年级': '星期四',
}

# 人数限制：普通社团 15-32，精品社团 10-30
MIN_STUDENTS = 15
MAX_STUDENTS_ORDINARY = 32
MAX_STUDENTS_PREMIUM = 30
MAX_STUDENTS_DEFAULT = 30

# ----- 班级填报配额规则 -----
# 普通项目：每个班对该项目需填报 2~4 名学生（不足2不能提交；超出4当下拦截）
# 精品项目：每个班不强制填报、不限每班人数（自愿/全校自由报名）
PERCLASS_MIN_EXTRA = 2      # 每班每普通项目最少填报人数
PERCLASS_MAX_EXTRA = 4      # 每班每普通项目最多填报人数

# 普通社团项目模板：分类 -> 项目名列表
ORDINARY_PROJECTS = {
    '体育': ['足球', '篮球', '羽毛球', '乒乓球', '跳绳'],
    '艺术': ['合唱', '乐器', '舞蹈', '演说', '书法', '绘画', '泥塑', '剪纸'],
    '文化': ['阅读与理解', '写作与鉴赏', '学科思维训练', '口语交际'],
    '科技': ['科创发明', '实验与探究', '速叠与魔方', '信息技术'],
    '益智游戏': ['五子棋', '军棋', '象棋', '围棋', '跳棋', '24点', '三国杀', '狼人杀'],
}

# 教师选课限制：互斥年级对（同一教师不能教的两个年级组合）
# 因为一、四同周二上课；二、五同周三；三、六同周四
GRADE_MUTEX_PAIRS = [('一年级', '四年级'), ('二年级', '五年级'), ('三年级', '六年级')]

# 一个年级最多允许被教师任教的普通项目数（满额后该年级不再允许教师选项目）
MAX_PROJECTS_PER_GRADE = 17

# 上课地点池：按上课日（星期）划分。每个项目只能选一个唯一地点，
# 同一天内已被其他项目占用的地点不能再选；运动场可重复（不限）。
# 公共教室（音乐/美术/科学实验室/劳技/信息科技/科技活动/心理咨询室）跨天复用（错峰）。
LOCATIONS_BY_DAY = {
    '星期二': ['1.1','1.2','1.3','1.4','1.5','1.6','1.7',
              '4.1','4.2','4.3','4.4','4.5','4.6','4.7'],
    '星期三': ['2.1','2.2','2.3','2.4','2.5','2.6','2.7','2.8',
              '5.1','5.2','5.3','5.4','5.5','5.6','5.7','5.8'],
    '星期四': ['3.1','3.2','3.3','3.4','3.5','3.6','3.7','3.8',
              '6.1','6.2','6.3','6.4','6.5','6.6','6.7'],
}
# 各上课日共用的公共教室
COMMON_LOCATIONS = ['音乐室1','音乐室2','音乐室3','音乐室4','音乐室5',
                    '美术室1','美术室2','美术室3','美术室4','美术室5',
                    '科学实验室1','科学实验室2','劳技室1',
                    '4楼信息科技室','5楼信息科技室','科技活动室','心理咨询室']
# 可重复选择（不限次数）的地点
REPEATABLE_LOCATIONS = {'运动场'}

def locations_for_day(day):
    """返回某上课日可选的唯一地点列表（不含可重复地点）"""
    return (LOCATIONS_BY_DAY.get(day, []) + COMMON_LOCATIONS)


# ---------- 数据库 ----------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS admin_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS class_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        class_name TEXT UNIQUE NOT NULL,
        grade TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        teacher TEXT DEFAULT '',       -- 任课教师
        location TEXT DEFAULT '',      -- 上课地点
        schedule TEXT DEFAULT ''       -- 上课时间
    );

    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        class_id INTEGER NOT NULL,
        student_name TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(class_id, student_name),
        FOREIGN KEY (class_id) REFERENCES class_accounts(id)
    );

    CREATE TABLE IF NOT EXISTS clubs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('ordinary','premium')),
        grade TEXT,                    -- 普通社团所属年级；精品社团为 NULL（全校共用）
        description TEXT DEFAULT '',
        teacher TEXT DEFAULT '',       -- 授课教师
        location TEXT DEFAULT '',      -- 上课地点
        schedule TEXT DEFAULT '',      -- 上课时间
        max_students INTEGER DEFAULT 30,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        club_id INTEGER NOT NULL,
        class_id INTEGER NOT NULL,
        student_name TEXT NOT NULL,
        grade TEXT NOT NULL,
        status TEXT DEFAULT 'pending',   -- pending 待审核 / approved 已通过 / rejected 已拒绝
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (club_id) REFERENCES clubs(id),
        FOREIGN KEY (class_id) REFERENCES class_accounts(id)
    );

    -- 班级报名提交记录：班主任填齐后点"提交本班报名"才生成。
    -- 只有已提交的班级，其报名记录才算进入管理员审核范围。
    CREATE TABLE IF NOT EXISTS class_submissions (
        class_id INTEGER PRIMARY KEY,
        grade TEXT NOT NULL,
        submitted_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (class_id) REFERENCES class_accounts(id)
    );

    -- 教师账号（功能一：教师登录并选课）
    CREATE TABLE IF NOT EXISTS teachers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        subjects TEXT DEFAULT '',        -- 擅长的学科，逗号分隔，如 "语文,书法"
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );

    -- 教师任教社团关联（一名教师可任教多个社团）
    CREATE TABLE IF NOT EXISTS teacher_clubs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER NOT NULL,
        club_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(teacher_id, club_id),
        FOREIGN KEY (teacher_id) REFERENCES teachers(id),
        FOREIGN KEY (club_id) REFERENCES clubs(id)
    );

    -- 巡课人员账号（功能二：巡课记录）
    CREATE TABLE IF NOT EXISTS inspectors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );

    -- 巡课记录（哪一天哪些人在哪里上课 + 评价反馈）
    CREATE TABLE IF NOT EXISTS inspections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inspection_date TEXT NOT NULL,   -- 巡课日期 YYYY-MM-DD
        club_id INTEGER NOT NULL,
        inspector TEXT NOT NULL,         -- 巡课人姓名
        location TEXT DEFAULT '',        -- 实际巡课地点（上课地点快照）
        teacher TEXT DEFAULT '',         -- 授课教师快照
        schedule TEXT DEFAULT '',        -- 上课时间快照
        rating INTEGER DEFAULT 3,        -- 评价星级 1-5
        comment TEXT DEFAULT '',         -- 评价反馈
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (club_id) REFERENCES clubs(id)
    );
    ''')
    # 迁移：为已存在的 clubs 表补充新增字段（幂等）
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(clubs)").fetchall()]
        for col, ddl in [('teacher', 'TEXT DEFAULT \'\''),
                         ('location', 'TEXT DEFAULT \'\''),
                         ('schedule', 'TEXT DEFAULT \'\''),
                         ('category', 'TEXT DEFAULT \'\'')]:
            if col not in cols:
                db.execute(f'ALTER TABLE clubs ADD COLUMN {col} {ddl}')
    except Exception:
        pass

    # 迁移：为已存在的 class_accounts 表补充任课教师/地点/时间字段（幂等）
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(class_accounts)").fetchall()]
        for col, ddl in [('teacher', 'TEXT DEFAULT \'\''),
                         ('location', 'TEXT DEFAULT \'\''),
                         ('schedule', 'TEXT DEFAULT \'\'')]:
            if col not in cols:
                db.execute(f'ALTER TABLE class_accounts ADD COLUMN {col} {ddl}')
    except Exception:
        pass

    # 默认管理员
    cur = db.execute("SELECT COUNT(*) FROM admin_users WHERE username='admin'")
    if cur.fetchone()[0] == 0:
        db.execute("INSERT INTO admin_users (username, password) VALUES (?, ?)",
                   ('admin', 'admin123'))

    # 标准普通社团体系重建/补齐：
    # 若 clubs 表里尚无任何"年级×分类"新课标社团（旧体系 category 为空），
    # 则清除旧社团及其关联数据，按 年级(一~六) × 分类 全量重建；
    # 若已有新课标社团，则仅增量补齐缺失的（年级×项目）组合（不删已有数据，幂等）。
    try:
        cols2 = [r[1] for r in db.execute("PRAGMA table_info(clubs)").fetchall()]
        if 'category' in cols2:
            new_cnt = db.execute("SELECT COUNT(*) FROM clubs WHERE category<>''").fetchone()[0]
            if new_cnt == 0:
                # 全量重建：清除旧社团及其关联数据（用户要求清除所有社团项目后重建）
                db.execute("DELETE FROM registrations")
                db.execute("DELETE FROM inspections")
                db.execute("DELETE FROM teacher_clubs")
                db.execute("DELETE FROM clubs")
                for grade in GRADES:
                    for cat, projects in ORDINARY_PROJECTS.items():
                        for proj in projects:
                            db.execute('''
                                INSERT INTO clubs
                                (name, type, grade, category, description, schedule, max_students)
                                VALUES (?, 'ordinary', ?, ?, ?, ?, ?)
                            ''', (f'{grade}{proj}', grade, cat,
                                  f'{grade}{proj}（{cat}类）',
                                  GRADE_SCHEDULE[grade], 32))
            else:
                # 增量补齐：检查每个（年级×项目）是否已存在，缺失则插入
                existing = set()
                for r in db.execute(
                        "SELECT grade, name FROM clubs WHERE type='ordinary' AND category<>''"
                ).fetchall():
                    # 名称格式为 年级+项目，反推项目名
                    g = r['grade']
                    nm = r['name']
                    if g and nm and nm.startswith(g):
                        existing.add((g, nm[len(g):]))
                for grade in GRADES:
                    for cat, projects in ORDINARY_PROJECTS.items():
                        for proj in projects:
                            if (grade, proj) not in existing:
                                db.execute('''
                                    INSERT INTO clubs
                                    (name, type, grade, category, description, schedule, max_students)
                                    VALUES (?, 'ordinary', ?, ?, ?, ?, ?)
                                ''', (f'{grade}{proj}', grade, cat,
                                      f'{grade}{proj}（{cat}类）',
                                      GRADE_SCHEDULE[grade], 32))
    except Exception:
        pass

    db.commit()
    db.close()


# ---------- 工具函数 ----------
def login_required():
    if not session.get('role'):
        return redirect(url_for('index'))
    return None


def count_registrations(db, club_id, status=None):
    if status:
        cur = db.execute("SELECT COUNT(*) FROM registrations WHERE club_id=? AND status=?",
                         (club_id, status))
    else:
        cur = db.execute("SELECT COUNT(*) FROM registrations WHERE club_id=? AND status IN ('pending','approved')",
                         (club_id,))
    return cur.fetchone()[0]


def count_class_club(db, class_id, club_id):
    """该班级在某项目的报名人数（不含已拒绝）。"""
    cur = db.execute(
        "SELECT COUNT(*) FROM registrations WHERE class_id=? AND club_id=? AND status != 'rejected'",
        (class_id, club_id))
    return cur.fetchone()[0]


def class_fill_progress(db, class_id, grade):
    """返回班主任端"班级填报进度"数据。

    只统计本班该年级【当前开放】的普通项目（精品全校自愿不强制）。
    每项返回 dict: club_id, name, category, count, state
      state: ok=人数2-4 合格 / short=不足2 / excess=超过4 / none=暂无本班学生
    """
    clubs = db.execute('''
        SELECT id, name, category FROM clubs
        WHERE is_active=1 AND type='ordinary' AND grade=?
        ORDER BY id
    ''', (grade,)).fetchall()
    items = []
    for c in clubs:
        cnt = count_class_club(db, class_id, c['id'])
        # 状态：0=未填报(灰)；1=不足2(灰)；2-4=合格(绿)；>4=超限(红,仅管理员导入可能出现)
        if cnt == 0:
            state = 'none'
        elif cnt < PERCLASS_MIN_EXTRA:
            state = 'short'
        elif cnt <= PERCLASS_MAX_EXTRA:
            state = 'ok'
        else:
            state = 'excess'
        items.append({'club_id': c['id'], 'name': c['name'],
                      'category': c['category'], 'count': cnt, 'state': state})
    return items


def class_submittable(items):
    """班级能否提交：所有开放普通项目都满足 2~4 人。返回 (ok, 未达标项列表)。"""
    bad = [it for it in items if it['state'] != 'ok']
    return (len(bad) == 0), bad


def validate_max_students(ctype, value):
    """校验并返回人数上限：普通社团 15-32，精品社团 10-30。
    返回 (ok, max_students 或 None)"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False, None
    if ctype == 'premium':
        return (10 <= n <= 30), n
    return (MIN_STUDENTS <= n <= MAX_STUDENTS_ORDINARY), n


# ---------- 公共页面 ----------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        db = get_db()

        # 管理员
        a = db.execute("SELECT * FROM admin_users WHERE username=?", (username,)).fetchone()
        if a and a['password'] == password:
            session['role'] = 'admin'
            session['username'] = a['username']
            session['admin_id'] = a['id']
            return redirect(url_for('admin_dashboard'))

        # 班级账号
        c = db.execute("SELECT * FROM class_accounts WHERE username=?", (username,)).fetchone()
        if c and c['password'] == password:
            session['role'] = 'teacher'
            session['username'] = c['username']
            session['class_id'] = c['id']
            session['class_name'] = c['class_name']
            session['grade'] = c['grade']
            return redirect(url_for('teacher_dashboard'))

        # 教师账号（授课教师选课）
        t = db.execute("SELECT * FROM teachers WHERE username=?", (username,)).fetchone()
        if t and t['password'] == password:
            session['role'] = 'staff'
            session['username'] = t['username']
            session['teacher_id'] = t['id']
            session['teacher_name'] = t['name']
            return redirect(url_for('teacher_home'))

        # 巡课人员账号
        ins = db.execute("SELECT * FROM inspectors WHERE username=?", (username,)).fetchone()
        if ins and ins['password'] == password:
            session['role'] = 'inspector'
            session['username'] = ins['username']
            session['inspector_name'] = ins['name']
            session['inspector_id'] = ins['id']
            return redirect(url_for('inspector_dashboard'))

        flash('账号或密码错误', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ---------- 班主任（前端） ----------
@app.route('/teacher')
def teacher_dashboard():
    if not session.get('role') == 'teacher':
        return redirect(url_for('login'))
    db = get_db()
    grade = session['grade']
    class_id = session['class_id']

    # 该班主任已报名的学生
    regs = db.execute('''
        SELECT r.*, c.name AS club_name, c.type AS club_type,
               c.teacher AS club_teacher, c.location AS club_location,
               c.schedule AS club_schedule, c.description AS club_description
        FROM registrations r JOIN clubs c ON r.club_id = c.id
        WHERE r.class_id=? AND r.status != 'rejected'
        ORDER BY r.id DESC
    ''', (class_id,)).fetchall()

    # 可选社团：本年级普通社团 + 全校精品社团（且启用）
    clubs = db.execute('''
        SELECT * FROM clubs
        WHERE is_active=1 AND (type='premium' OR (type='ordinary' AND grade=?))
        ORDER BY type DESC, id
    ''', (grade,)).fetchall()

    # 每个社团的报名人数与状态（全校维度 + 本班维度）
    club_info = {}
    for c in clubs:
        cnt = count_registrations(db, c['id'])
        class_cnt = count_class_club(db, class_id, c['id'])
        club_info[c['id']] = {
            'count': cnt,
            'class_count': class_cnt,
            'full': cnt >= c['max_students'],
            # 普通项目：本班是否已达每班上限（>不能再加）
            'class_full': (c['type'] == 'ordinary') and (class_cnt >= PERCLASS_MAX_EXTRA),
        }

    # 班级填报进度：本班当前开放普通项目的人数达标情况（决定能否提交）
    fill_items = class_fill_progress(db, class_id, grade)
    submittable, bad_items = class_submittable(fill_items)

    # 本班学生名单（管理员批量导入的）
    students = db.execute('''
        SELECT s.*,
          (SELECT COUNT(*) FROM registrations r
           WHERE r.student_name=s.student_name AND r.class_id=s.class_id AND r.status != 'rejected') AS has_reg
        FROM students s WHERE s.class_id=? ORDER BY s.id
    ''', (class_id,)).fetchall()

    # 本班任课教师/上课地点/上课时间（由 CSV 导入时填写）
    cls = db.execute("SELECT * FROM class_accounts WHERE id=?", (class_id,)).fetchone()

    # 本班是否已成功提交（进入待管理员审核）
    sub = db.execute("SELECT submitted_at FROM class_submissions WHERE class_id=?",
                     (class_id,)).fetchone()
    cls_submitted = bool(sub)

    return render_template('teacher.html', regs=regs, clubs=clubs,
                           cls_submitted=cls_submitted,
                           club_info=club_info, grade=grade,
                           class_name=session['class_name'], students=students,
                           cls=cls, fill_items=fill_items,
                           submittable=submittable, bad_items=bad_items,
                           PERCLASS_MIN_EXTRA=PERCLASS_MIN_EXTRA,
                           PERCLASS_MAX_EXTRA=PERCLASS_MAX_EXTRA)


@app.route('/teacher/submit', methods=['POST'])
def teacher_submit():
    if not session.get('role') == 'teacher':
        return redirect(url_for('login'))
    club_id = request.form.get('club_id')
    student_name = request.form.get('student_name', '').strip()
    db = get_db()

    if not club_id or not student_name:
        flash('请选择社团并填写学生姓名', 'danger')
        return redirect(url_for('teacher_dashboard'))

    club = db.execute("SELECT * FROM clubs WHERE id=? AND is_active=1", (club_id,)).fetchone()
    if not club:
        flash('社团不存在或已停用', 'danger')
        return redirect(url_for('teacher_dashboard'))

    # 权限：精品可报；普通社团需属于本年级
    grade = session['grade']
    if club['type'] == 'ordinary' and club['grade'] != grade:
        flash('无权报名该社团', 'danger')
        return redirect(url_for('teacher_dashboard'))

    # 该学生是否已报名（全系统查，避免重复）
    dup = db.execute('''
        SELECT COUNT(*) FROM registrations r JOIN clubs c ON r.club_id=c.id
        WHERE r.student_name=? AND r.class_id=? AND r.status != 'rejected'
    ''', (student_name, session['class_id'])).fetchone()[0]
    if dup:
        flash(f'学生 "{student_name}" 已经报名过了，一个学生只能报一个社团', 'danger')
        return redirect(url_for('teacher_dashboard'))

    # 人数上限检查（全校级：每社团人数上限）
    cnt = count_registrations(db, club['id'])
    if cnt >= club['max_students']:
        flash(f'该社团已满员（{club["max_students"]}人），不能再报名', 'danger')
        return redirect(url_for('teacher_dashboard'))

    # 班级填报配额检查：普通项目每个班最多 PERCLASS_MAX_EXTRA 人（当下拦截第 5 个）
    if club['type'] == 'ordinary':
        class_cnt = count_class_club(db, session['class_id'], club['id'])
        if class_cnt >= PERCLASS_MAX_EXTRA:
            flash(f'本班在「{club["name"]}」已达上限 {PERCLASS_MAX_EXTRA} 人，不能再为本班添加该项目的学生', 'danger')
            return redirect(url_for('teacher_dashboard'))

    db.execute('''
        INSERT INTO registrations (club_id, class_id, student_name, grade, status)
        VALUES (?,?,?,?,?)
    ''', (club['id'], session['class_id'], student_name, grade, 'pending'))
    db.commit()
    flash(f'学生 "{student_name}" 报名成功，等待管理员审核', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/remove/<int:rid>', methods=['POST'])
def teacher_remove(rid):
    if not session.get('role') == 'teacher':
        return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM registrations WHERE id=? AND class_id=?",
               (rid, session['class_id']))
    db.commit()
    flash('已撤销该报名', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/submit-class', methods=['POST'])
def teacher_submit_class():
    """班主任点「提交本班报名」：校验本班开放普通项目是否全部达标(每项2-4人)。
    达标=生成/更新提交记录(进入待管理员审核)；不达标=打回并列出缺项。"""
    if not session.get('role') == 'teacher':
        return redirect(url_for('login'))
    db = get_db()
    class_id = session['class_id']
    grade = session['grade']
    items = class_fill_progress(db, class_id, grade)
    ok, bad = class_submittable(items)
    failcount = len([it for it in items if it['state'] == 'none' or it['state'] == 'short'])
    if not ok:
        detail = []
        for it in bad:
            label = ('未填报' if it['count'] == 0
                     else '不足2人' if it['state'] == 'short'
                     else '超过4人')
            detail.append(f'「{it["name"]}」当前 {it["count"]} 人（{label}）')
        flash(f'提交未通过：以下 {len(bad)} 个项目未达标，请补齐后再提交。' + '；'.join(detail), 'danger')
        return redirect(url_for('teacher_dashboard'))
    # 达标：记录提交
    db.execute('''
        INSERT INTO class_submissions (class_id, grade, submitted_at)
        VALUES (?, ?, datetime('now','localtime'))
        ON CONFLICT(class_id) DO UPDATE SET submitted_at=datetime('now','localtime'), grade=excluded.grade
    ''', (class_id, grade))
    db.commit()
    flash('✅ 本班报名提交成功！共覆盖 %d 个开放普通项目（每项 %d-%d 人），已进入管理员审核。'
          % (len(items), PERCLASS_MIN_EXTRA, PERCLASS_MAX_EXTRA), 'success')
    return redirect(url_for('teacher_dashboard'))


# ================= 功能一：教师登录并选课 =================
@app.route('/teacher_home')
def teacher_home():
    """教师主页：查看可选课程（按擅长学科筛选）、已任教课程、学生名单"""
    if not session.get('role') == 'staff':
        return redirect(url_for('login'))
    db = get_db()
    teacher_id = session['teacher_id']
    teacher = db.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
    if not teacher:
        flash('教师账号不存在', 'danger')
        return redirect(url_for('logout'))

    # 擅长学科关键词
    subjects = [s.strip() for s in (teacher['subjects'] or '').split(',') if s.strip()]

    # 已任教的社团（含关联）
    my_clubs = db.execute('''
        SELECT c.*,
          (SELECT COUNT(*) FROM registrations r
           WHERE r.club_id=c.id AND r.status IN ('pending','approved')) AS cnt
        FROM teacher_clubs tc JOIN clubs c ON tc.club_id = c.id
        WHERE tc.teacher_id=? ORDER BY c.id
    ''', (teacher_id,)).fetchall()

    # 每个任教社团的学生名单（已通过/待审核的报名）
    my_club_students = {}
    for c in my_clubs:
        rows = db.execute('''
            SELECT r.student_name, r.grade, r.status, a.class_name
            FROM registrations r
            JOIN class_accounts a ON r.class_id = a.id
            WHERE r.club_id=? AND r.status IN ('pending','approved')
            ORDER BY r.grade, a.class_name, r.student_name
        ''', (c['id'],)).fetchall()
        my_club_students[c['id']] = rows

    # 可选的社团：未任教 + 启用 + 尚无教师负责（一个项目只能由一位教师任教）
    # 支持按 年级 / 分类 筛选
    sel_grade = request.args.get('grade', '').strip()
    sel_category = request.args.get('category', '').strip()

    q = '''
        SELECT c.*,
          (SELECT COUNT(*) FROM registrations r
           WHERE r.club_id=c.id AND r.status IN ('pending','approved')) AS cnt
        FROM clubs c WHERE c.is_active=1
          AND c.id NOT IN (SELECT club_id FROM teacher_clubs WHERE teacher_id=?)
          AND c.id NOT IN (SELECT club_id FROM teacher_clubs WHERE teacher_id<>?)
          AND (c.teacher IS NULL OR c.teacher='' OR c.teacher=?)
    '''
    params = [teacher_id, teacher_id, teacher['name']]
    if sel_grade == 'all':
        # 只看精品社团（全校性）
        q += ' AND c.type=\'premium\''
    elif sel_grade:
        q += ' AND c.grade=?'
        params.append(sel_grade)
    if sel_category and sel_category != 'all':
        q += ' AND c.category=?'
        params.append(sel_category)
    q += ' ORDER BY c.type DESC, c.' + ('grade' if sel_grade and sel_grade != 'all' else 'id')
    available = db.execute(q, params).fetchall()

    # 判断每个社团是否与擅长学科匹配（用于标注推荐）
    def matches(c):
        if not subjects:
            return False
        hay = (c['name'] or '') + (c['description'] or '')
        for s in subjects:
            if s and s in hay:
                return True
        # 授课教师名匹配（教师可接自己挂名的社团）
        if c['teacher'] and c['teacher'] == teacher['name']:
            return True
        return False

    recommended_ids = [c['id'] for c in available if matches(c)]

    # 教师已任教社团的年级集合（用于前端提示互斥规则）
    my_grades = sorted({c['grade'] for c in my_clubs if c['grade']})

    # 各年级已被教师任教的普通项目数（用于前端显示"已认领 X/17"名额状态）
    grade_taken = {}
    for g in GRADES:
        n = db.execute('''
            SELECT COUNT(*) FROM teacher_clubs tc JOIN clubs c ON tc.club_id=c.id
            WHERE c.grade=? AND c.type='ordinary'
        ''', (g,)).fetchone()[0]
        grade_taken[g] = n

    # 各上课日的可选地点池（供教师为任教课程选择上课地点）
    # 每个 ordinary 社团的上课日 = GRADE_SCHEDULE[grade]
    day_locations = {}
    day_taken = {}
    for day in ('星期二', '星期三', '星期四'):
        # 该日年级
        day_grades = [g for g, d in GRADE_SCHEDULE.items() if d == day]
        # 该日所有可选唯一地点
        day_locations[day] = list(REPEATABLE_LOCATIONS) + locations_for_day(day)
        # 该日已被占用的唯一地点（运动场不算）
        rows = db.execute('''
            SELECT location FROM clubs WHERE type='ordinary'
            AND grade IN (%s) AND location<>'' AND location NOT IN (%s)
        ''' % (','.join('?' * len(day_grades)),
               ','.join('?' * len(REPEATABLE_LOCATIONS))),
            day_grades + list(REPEATABLE_LOCATIONS)).fetchall()
        day_taken[day] = {r['location'] for r in rows}

    return render_template('teacher_home.html', teacher=teacher, subjects=subjects,
                           my_clubs=my_clubs, my_club_students=my_club_students,
                           available=available, recommended_ids=recommended_ids,
                           GRADES=GRADES, CATEGORIES=CATEGORIES,
                           sel_grade=sel_grade, sel_category=sel_category,
                           my_club_count=len(my_clubs), my_grades=my_grades,
                           GRADE_SCHEDULE=GRADE_SCHEDULE,
                           grade_taken=grade_taken, MAX_PROJECTS_PER_GRADE=MAX_PROJECTS_PER_GRADE,
                           day_locations=day_locations, day_taken=day_taken)


@app.route('/teacher/club/select/<int:cid>', methods=['POST'])
def teacher_club_select(cid):
    """教师选择任教某社团（最多任教 2 门）"""
    if not session.get('role') == 'staff':
        return redirect(url_for('login'))
    db = get_db()
    teacher_id = session['teacher_id']
    club = db.execute("SELECT * FROM clubs WHERE id=?", (cid,)).fetchone()
    if not club:
        flash('社团不存在', 'danger')
        return redirect(url_for('teacher_home'))
    # 已任教则跳过
    dup = db.execute("SELECT 1 FROM teacher_clubs WHERE teacher_id=? AND club_id=?",
                     (teacher_id, cid)).fetchone()
    if dup:
        flash('你已任教该社团', 'warning')
        return redirect(url_for('teacher_home'))
    # 一个项目只能由一位教师任教，不能被多人选择
    other = db.execute("SELECT 1 FROM teacher_clubs WHERE club_id=? AND teacher_id<>?",
                       (cid, teacher_id)).fetchone()
    if other:
        flash(f'「{club["name"]}」已由其他教师任教，一个项目只能由一位教师负责。', 'warning')
        return redirect(url_for('teacher_home'))
    if club['teacher'] and club['teacher'] != session.get('teacher_name'):
        flash(f'「{club["name"]}」已由 {club["teacher"]} 任教，一个项目只能由一位教师负责。', 'warning')
        return redirect(url_for('teacher_home'))
    # 最多任教 2 门
    cnt = db.execute("SELECT COUNT(*) FROM teacher_clubs WHERE teacher_id=?",
                     (teacher_id,)).fetchone()[0]
    if cnt >= 2:
        flash('每位教师最多只能任教 2 门课程。如需调整，请先在右侧退出已任教的课程。', 'warning')
        return redirect(url_for('teacher_home'))

    # 年级名额限制：一个年级最多允许 17 个项目被教师任教，满额后该年级不再允许教师选项目
    if club['grade']:
        grade_taken = db.execute('''
            SELECT COUNT(*) FROM teacher_clubs tc JOIN clubs c ON tc.club_id=c.id
            WHERE c.grade=? AND c.type='ordinary'
        ''', (club['grade'],)).fetchone()[0]
        if grade_taken >= MAX_PROJECTS_PER_GRADE:
            flash(f'「{club["grade"]}」的项目选课名额已满（{MAX_PROJECTS_PER_GRADE} 个项目已由教师任教），该年级暂不接受新选课。', 'warning')
            return redirect(url_for('teacher_home'))

    # 组合限制：不能教同一个年级的 2 个项目；不能教互斥年级组合（一四/二五/三六，因同日上课）
    if cnt >= 1 and club['grade']:
        my_club_rows = db.execute('''
            SELECT c.grade FROM teacher_clubs tc JOIN clubs c ON tc.club_id=c.id
            WHERE tc.teacher_id=?
        ''', (teacher_id,)).fetchall()
        for row in my_club_rows:
            g = row['grade']
            if not g:
                continue
            if g == club['grade']:
                flash(f'不能同时任教同一个年级（{club["grade"]}）的两个项目。请选择其他年级。', 'warning')
                return redirect(url_for('teacher_home'))
            # 互斥组合判断
            pair = {g, club['grade']}
            for (a, b) in GRADE_MUTEX_PAIRS:
                if pair == {a, b}:
                    flash(f'「{club["grade"]}」与「{g}」为同一天上课的互斥组合，不能由同一位教师同时任教。', 'warning')
                    return redirect(url_for('teacher_home'))
    try:
        db.execute("INSERT INTO teacher_clubs (teacher_id, club_id) VALUES (?,?)",
                   (teacher_id, cid))
        # 同步更新社团的授课教师字段（展示用）
        db.execute("UPDATE clubs SET teacher=? WHERE id=?",
                   (session.get('teacher_name', ''), cid))
        db.commit()
        flash(f'你已选择任教「{club["name"]}」', 'success')
    except sqlite3.IntegrityError:
        flash('你已任教该社团', 'warning')
    return redirect(url_for('teacher_home'))


@app.route('/teacher/club/create', methods=['POST'])
def teacher_club_create():
    """教师手动新增课程（全局可见，学生也能报名）"""
    if not session.get('role') == 'staff':
        return redirect(url_for('login'))
    db = get_db()
    teacher_id = session['teacher_id']
    teacher = db.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
    if not teacher:
        flash('教师账号不存在', 'danger')
        return redirect(url_for('teacher_home'))
    # 最多任教 2 门
    cnt = db.execute("SELECT COUNT(*) FROM teacher_clubs WHERE teacher_id=?",
                     (teacher_id,)).fetchone()[0]
    if cnt >= 2:
        flash('每位教师最多只能任教 2 门课程。如需新增，请先退出已任教的课程。', 'warning')
        return redirect(url_for('teacher_home'))

    name = request.form.get('name', '').strip()
    ctype_raw = request.form.get('type', 'ordinary')
    grade = request.form.get('grade', '').strip() or None
    description = request.form.get('description', '').strip()
    location = request.form.get('location', '').strip()
    schedule = request.form.get('schedule', '').strip()
    max_students = request.form.get('max_students', MAX_STUDENTS_DEFAULT)

    if not name:
        flash('请填写课程名称', 'danger')
        return redirect(url_for('teacher_home'))
    ctype = 'premium' if ctype_raw == 'premium' else 'ordinary'
    if ctype == 'ordinary' and (grade not in GRADES):
        flash('普通课程必须选择所属年级', 'danger')
        return redirect(url_for('teacher_home'))
    if ctype == 'premium':
        grade = None
    ok, max_students = validate_max_students(ctype, max_students)
    if not ok:
        if ctype == 'premium':
            flash('精品社团人数上限需在 10-30 之间', 'danger')
        else:
            flash('普通社团人数上限需在 15-32 之间', 'danger')
        return redirect(url_for('teacher_home'))

    # 组合限制：自创课程也受 2 门 + 同年级/互斥年级规则约束
    if grade:
        # 年级名额限制：一个年级最多允许 17 个项目被教师任教
        grade_taken = db.execute('''
            SELECT COUNT(*) FROM teacher_clubs tc JOIN clubs c ON tc.club_id=c.id
            WHERE c.grade=? AND c.type='ordinary'
        ''', (grade,)).fetchone()[0]
        if grade_taken >= MAX_PROJECTS_PER_GRADE:
            flash(f'「{grade}」的项目选课名额已满（{MAX_PROJECTS_PER_GRADE} 个项目已由教师任教），该年级暂不接受新选课。', 'warning')
            return redirect(url_for('teacher_home'))
        my_club_rows = db.execute('''
            SELECT c.grade FROM teacher_clubs tc JOIN clubs c ON tc.club_id=c.id
            WHERE tc.teacher_id=?
        ''', (teacher_id,)).fetchall()
        for row in my_club_rows:
            g = row['grade']
            if not g:
                continue
            if g == grade:
                flash(f'不能同时任教同一个年级（{grade}）的两个项目。请选择其他年级。', 'warning')
                return redirect(url_for('teacher_home'))
            for (a, b) in GRADE_MUTEX_PAIRS:
                if {g, grade} == {a, b}:
                    flash(f'「{grade}」与「{g}」为同一天上课的互斥组合，不能由同一位教师同时任教。', 'warning')
                    return redirect(url_for('teacher_home'))

    cur = db.execute('''
        INSERT INTO clubs (name, type, grade, description, teacher, location, schedule, max_students)
        VALUES (?,?,?,?,?,?,?,?)
    ''', (name, ctype, grade, description, teacher['name'], location, schedule, max_students))
    club_id = cur.lastrowid
    # 教师自动任教新建的课程
    db.execute("INSERT INTO teacher_clubs (teacher_id, club_id) VALUES (?,?)",
               (teacher_id, club_id))
    db.commit()
    flash(f'课程「{name}」已创建并归你任教（可被学生报名）', 'success')
    return redirect(url_for('teacher_home'))


@app.route('/teacher/club/leave/<int:cid>', methods=['POST'])
def teacher_club_leave(cid):
    """教师退出任教某社团"""
    if not session.get('role') == 'staff':
        return redirect(url_for('login'))
    db = get_db()
    teacher_id = session['teacher_id']
    club = db.execute("SELECT * FROM clubs WHERE id=?", (cid,)).fetchone()
    db.execute("DELETE FROM teacher_clubs WHERE teacher_id=? AND club_id=?",
               (teacher_id, cid))
    # 若该社团的展示教师正是本人，则清空展示字段（避免误导）
    if club and club['teacher'] == session.get('teacher_name'):
        db.execute("UPDATE clubs SET teacher='' WHERE id=?", (cid,))
    db.commit()
    flash(f'你已退出「{club["name"] if club else ""}」', 'success')
    return redirect(url_for('teacher_home'))


@app.route('/teacher/club/location/<int:cid>', methods=['POST'])
def teacher_club_set_location(cid):
    """教师为自己任教的普通社团选择唯一上课地点。
    - 地点只能从该社团上课日（由年级经 GRADE_SCHEDULE 推出）的可用池中选择
    - 同一天内已被其他项目占用的地点（运动场除外）不能再选
    """
    if not session.get('role') == 'staff':
        return redirect(url_for('login'))
    db = get_db()
    teacher_id = session['teacher_id']
    club = db.execute("SELECT * FROM clubs WHERE id=?", (cid,)).fetchone()
    if not club:
        flash('社团不存在', 'danger')
        return redirect(url_for('teacher_home'))
    # 必须是本人任教的社团
    mine = db.execute("SELECT 1 FROM teacher_clubs WHERE teacher_id=? AND club_id=?",
                      (teacher_id, cid)).fetchone()
    if not mine:
        flash('只能为你任教的课程选择地点', 'warning')
        return redirect(url_for('teacher_home'))
    # 仅普通社团需要唯一地点（精品为全校，可由管理员安排，此处不做唯一约束）
    if club['type'] != 'ordinary':
        flash('精品社团地点由管理员统一安排', 'warning')
        return redirect(url_for('teacher_home'))

    loc = request.form.get('location', '').strip()
    day = GRADE_SCHEDULE.get(club['grade'])
    if not day:
        flash('无法确定该课程的上课日', 'warning')
        return redirect(url_for('teacher_home'))
    if loc in REPEATABLE_LOCATIONS:
        # 运动场等可重复地点，直接保存
        db.execute("UPDATE clubs SET location=? WHERE id=?", (loc, cid))
        db.commit()
        flash(f'已为「{club["name"]}」选择上课地点：{loc}', 'success')
        return redirect(url_for('teacher_home'))
    pool = locations_for_day(day)
    if loc not in pool:
        flash(f'「{loc}」不是{day}的可选地点，请从该日地点池中选择。', 'warning')
        return redirect(url_for('teacher_home'))
    # 唯一性：同上课日下，非重复地点已被其他普通社团占用则不可选
    day_grades = [g for g, d in GRADE_SCHEDULE.items() if d == day]
    taken = db.execute('''
        SELECT COUNT(*) FROM clubs
        WHERE type='ordinary' AND grade IN (%s) AND location=? AND id<>?
    ''' % ','.join('?' * len(day_grades)),
        day_grades + [loc, cid]).fetchone()[0]
    if taken > 0:
        flash(f'「{loc}」在{day}已被其他项目占用，请选择其他地点。', 'warning')
        return redirect(url_for('teacher_home'))
    db.execute("UPDATE clubs SET location=? WHERE id=?", (loc, cid))
    db.commit()
    flash(f'已为「{club["name"]}」选择上课地点：{loc}', 'success')
    return redirect(url_for('teacher_home'))


@app.route('/teacher/club/edit/<int:cid>', methods=['POST'])
def teacher_club_edit(cid):
    """授课教师编辑自己任教的社团内容（项目名称、上课地点等）"""
    if not session.get('role') == 'staff':
        return redirect(url_for('login'))
    db = get_db()
    teacher_id = session['teacher_id']
    teacher = db.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
    if not teacher:
        flash('教师账号不存在', 'danger')
        return redirect(url_for('teacher_home'))

    # 权限：只能编辑自己任教的社团
    own = db.execute("SELECT 1 FROM teacher_clubs WHERE teacher_id=? AND club_id=?",
                     (teacher_id, cid)).fetchone()
    if not own:
        flash('你只能编辑自己任教的社团', 'danger')
        return redirect(url_for('teacher_home'))
    club = db.execute("SELECT * FROM clubs WHERE id=?", (cid,)).fetchone()
    if not club:
        flash('社团不存在', 'danger')
        return redirect(url_for('teacher_home'))

    name = request.form.get('name', '').strip()
    location = request.form.get('location', '').strip()
    schedule = request.form.get('schedule', '').strip()
    description = request.form.get('description', '').strip()

    if not name:
        flash('课程名称不能为空', 'danger')
        return redirect(url_for('teacher_home'))

    # 普通社团上课地点唯一性校验（防止编辑时绕过选地点规则）
    if club['type'] == 'ordinary' and location and location not in REPEATABLE_LOCATIONS and club['grade']:
        day = GRADE_SCHEDULE.get(club['grade'])
        if day:
            pool = locations_for_day(day)
            if location not in pool:
                flash(f'「{location}」不是{day}的可选地点，请从该日地点池中选择。', 'warning')
                return redirect(url_for('teacher_home'))
            day_grades = [g for g, d in GRADE_SCHEDULE.items() if d == day]
            taken = db.execute('''
                SELECT COUNT(*) FROM clubs
                WHERE type='ordinary' AND grade IN (%s) AND location=? AND id<>?
            ''' % ','.join('?' * len(day_grades)),
                day_grades + [location, cid]).fetchone()[0]
            if taken > 0:
                flash(f'「{location}」在{day}已被其他项目占用，请选择其他地点。', 'warning')
                return redirect(url_for('teacher_home'))

    db.execute('''
        UPDATE clubs SET name=?, location=?, schedule=?, description=? WHERE id=?
    ''', (name, location, schedule, description, cid))
    db.commit()
    flash(f'课程「{name}」内容已更新', 'success')
    return redirect(url_for('teacher_home'))


# ================= 功能二：巡课记录 =================
@app.route('/inspector')
def inspector_dashboard():
    """巡课人员主页：按日期查看当天哪些人在哪里上课，并可评价反馈"""
    if not session.get('role') == 'inspector':
        return redirect(url_for('login'))
    db = get_db()
    inspector_name = session.get('inspector_name', '巡课人员')

    # 巡课日期过滤（默认今天）
    today = datetime.now().strftime('%Y-%m-%d')
    sel_date = request.args.get('date', today)
    try:
        datetime.strptime(sel_date, '%Y-%m-%d')
    except ValueError:
        sel_date = today

    # 上课时间 / 上课地点 筛选（功能二优化：可筛选巡课时间、上课地点）
    sel_time = request.args.get('time', '').strip()
    sel_location = request.args.get('location', '').strip()

    # 当日排课：有上课地点/时间的启用社团；未排课地点时间的也列出（可巡课）
    query = '''
        SELECT c.*,
          (SELECT COUNT(*) FROM registrations r
           WHERE r.club_id=c.id AND r.status IN ('pending','approved')) AS cnt
        FROM clubs c WHERE c.is_active=1
    '''
    params = []
    if sel_time:
        query += ' AND c.schedule LIKE ?'
        params.append('%' + sel_time + '%')
    if sel_location:
        query += ' AND c.location LIKE ?'
        params.append('%' + sel_location + '%')
    query += ' ORDER BY c.type DESC, c.id'
    day_clubs = db.execute(query, params).fetchall()

    # 所有可选地点/时间（用于筛选下拉）——来自现有社团
    all_locations = [r[0] for r in db.execute(
        "SELECT DISTINCT location FROM clubs WHERE is_active=1 AND location<>'' ORDER BY location").fetchall()]
    all_times = [r[0] for r in db.execute(
        "SELECT DISTINCT schedule FROM clubs WHERE is_active=1 AND schedule<>'' ORDER BY schedule").fetchall()]

    # 已存在的巡课记录（该日）
    existing = {}
    for r in db.execute("SELECT * FROM inspections WHERE inspection_date=?", (sel_date,)).fetchall():
        existing[r['club_id']] = r

    # 汇总当天打卡：社团 -> (人数, 教师, 地点, 时间, 该日巡课记录)
    schedule = []
    for c in day_clubs:
        schedule.append({
            'club': c,
            'count': c['cnt'],
            'inspection': existing.get(c['id']),
        })

    total_clubs = len(schedule)
    inspected = sum(1 for s in schedule if s['inspection'])
    return render_template('inspector.html', schedule=schedule, sel_date=sel_date,
                           today=today, inspector_name=inspector_name,
                           total_clubs=total_clubs, inspected=inspected,
                           sel_time=sel_time, sel_location=sel_location,
                           all_locations=all_locations, all_times=all_times)


@app.route('/inspector/submit', methods=['POST'])
def inspector_submit():
    """提交巡课评价反馈（可同时修改该社团的任课教师）；支持 AJAX 实时更新"""
    if not session.get('role') == 'inspector':
        return jsonify(ok=False, msg='未登录或权限不足'), 403
    db = get_db()
    inspection_date = request.form.get('inspection_date', '').strip()
    club_id = request.form.get('club_id')
    rating = request.form.get('rating', '3')
    comment = request.form.get('comment', '').strip()
    new_teacher = request.form.get('teacher', '').strip()   # 可修改任课教师
    inspector_name = session.get('inspector_name', '巡课人员')
    is_ajax = request.form.get('ajax', '') == '1'

    def _fail(msg, code=400):
        if is_ajax:
            return jsonify(ok=False, msg=msg), code
        flash(msg, 'danger')
        return redirect(url_for('inspector_dashboard'))

    try:
        datetime.strptime(inspection_date, '%Y-%m-%d')
    except ValueError:
        return _fail('日期格式错误')

    club = db.execute("SELECT * FROM clubs WHERE id=?", (club_id,)).fetchone()
    if not club:
        return _fail('社团不存在')

    try:
        rating = int(rating)
        if not (1 <= rating <= 5):
            raise ValueError
    except ValueError:
        return _fail('评分需在 1-5 星之间')

    # 若巡课人填写了任课教师且与当前不同，则同步更新社团的授课教师字段
    teacher_saved = club['teacher'] or ''
    if new_teacher and new_teacher != (club['teacher'] or ''):
        db.execute("UPDATE clubs SET teacher=? WHERE id=?", (new_teacher, club_id))
        teacher_saved = new_teacher

    # 同一日同一社团重复巡课 -> 更新覆盖
    existing = db.execute(
        "SELECT id FROM inspections WHERE inspection_date=? AND club_id=?",
        (inspection_date, club_id)).fetchone()
    if existing:
        db.execute('''
            UPDATE inspections SET inspector=?, rating=?, comment=?, location=?, teacher=?, schedule=?
            WHERE id=?
        ''', (inspector_name, rating, comment,
              club['location'] or '', teacher_saved, club['schedule'] or '',
              existing['id']))
    else:
        db.execute('''
            INSERT INTO inspections
            (inspection_date, club_id, inspector, location, teacher, schedule, rating, comment)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (inspection_date, club_id, inspector_name,
              club['location'] or '', teacher_saved, club['schedule'] or '',
              rating, comment))
    db.commit()

    if is_ajax:
        return jsonify(ok=True, msg='巡课记录已更新', rating=rating,
                       comment=comment, inspector=inspector_name,
                       club_id=club_id, date=inspection_date)
    flash('巡课评价已提交', 'success')
    return redirect(url_for('inspector_dashboard', date=inspection_date))


# ---------- 后台（管理员） ----------
@app.route('/admin')
def admin_dashboard():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()

    # 筛选参数（GET，服务端过滤，示例：勾选"一年级"只看一年级的项目）
    f_grade = request.args.get('grade', '').strip()
    f_cat = request.args.get('category', '').strip()
    f_type = request.args.get('type', '').strip()
    where, params = [], []
    if f_grade:
        if f_grade == 'all':
            where.append("c.type='premium'")
        elif f_grade in GRADES:
            where.append("c.grade=?")
            params.append(f_grade)
    if f_cat in CATEGORIES:
        where.append("c.category=?")
        params.append(f_cat)
    if f_type == 'ordinary':
        where.append("c.type='ordinary'")
    elif f_type == 'premium':
        where.append("c.type='premium'")
    sql_where = ('WHERE ' + ' AND '.join(where)) if where else ''

    clubs = db.execute(f'''
        SELECT c.*,
          (SELECT COUNT(*) FROM registrations r WHERE r.club_id=c.id AND r.status IN ('pending','approved')) AS cnt
        FROM clubs c {sql_where} ORDER BY c.type DESC, c.id
    ''', params).fetchall()
    low_count = [c for c in clubs if c['cnt'] < MIN_STUDENTS]
    return render_template('admin.html', clubs=clubs, low_count=low_count,
                           MIN_STUDENTS=MIN_STUDENTS, GRADES=GRADES, CATEGORIES=CATEGORIES,
                           f_grade=f_grade, f_cat=f_cat, f_type=f_type)


@app.route('/admin/club/create', methods=['POST'])
def admin_club_create():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    name = request.form.get('name', '').strip()
    ctype = request.form.get('type')
    grade = request.form.get('grade') or None
    max_students = request.form.get('max_students', MAX_STUDENTS_DEFAULT)
    description = request.form.get('description', '').strip()
    teacher = request.form.get('teacher', '').strip()
    location = request.form.get('location', '').strip()
    schedule = request.form.get('schedule', '').strip()
    category = request.form.get('category', '').strip()
    if ctype == 'premium':
        grade = None
    db = get_db()

    if not name or ctype not in ('ordinary', 'premium'):
        flash('请填写社团名称并选择类型', 'danger')
        return redirect(url_for('admin_dashboard'))
    if ctype == 'ordinary' and grade not in GRADES:
        flash('普通社团必须选择所属年级', 'danger')
        return redirect(url_for('admin_dashboard'))
    ok, max_students = validate_max_students(ctype, max_students)
    if not ok:
        if ctype == 'premium':
            flash('精品社团人数上限需在 10-30 之间', 'danger')
        else:
            flash('普通社团人数上限需在 15-32 之间', 'danger')
        return redirect(url_for('admin_dashboard'))
    if ctype == 'ordinary' and category not in CATEGORIES:
        flash('普通社团必须选择所属分类（体育/艺术/文化/科技/益智游戏）', 'danger')
        return redirect(url_for('admin_dashboard'))

    db.execute('''
        INSERT INTO clubs (name, type, grade, category, description, teacher, location, schedule, max_students)
        VALUES (?,?,?,?,?,?,?,?,?)
    ''', (name, ctype, grade, category, description, teacher, location, schedule, max_students))
    db.commit()
    flash(f'社团 "{name}" 创建成功', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/club/toggle/<int:cid>', methods=['POST'])
def admin_club_toggle(cid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    db.execute("UPDATE clubs SET is_active = 1 - is_active WHERE id=?", (cid,))
    db.commit()
    return redirect(url_for('admin_dashboard'))


# 管理员：批量关闭/开启项目（勾选多个项目后统一设置开放/关闭）
# 参数：cids（多个社团id）、state（open=开放 / close=关闭）
@app.route('/admin/clubs/batch-toggle', methods=['POST'])
def admin_clubs_batch_toggle():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    ids = [i for i in request.form.getlist('cids') if i.isdigit()]
    state = request.form.get('state', '').strip()
    if not ids:
        flash('请先勾选要操作的项目', 'warning')
        return redirect(url_for('admin_dashboard'))
    if state == 'open':
        new_val = 1
        label = '开放'
    elif state == 'close':
        new_val = 0
        label = '关闭'
    else:
        flash('无效的操作状态', 'warning')
        return redirect(url_for('admin_dashboard'))
    placeholders = ','.join('?' * len(ids))
    db.execute(f"UPDATE clubs SET is_active=? WHERE id IN ({placeholders})", [new_val] + ids)
    db.commit()
    flash(f'已批量{label} {len(ids)} 个项目', 'success')
    return redirect(url_for('admin_dashboard'))


# 管理员：编辑单个社团内容（项目名称、类型、年级、授课教师、上课地点、上课时间、人数上限、简介）
@app.route('/admin/club/edit/<int:cid>', methods=['POST'])
def admin_club_edit(cid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    club = db.execute("SELECT * FROM clubs WHERE id=?", (cid,)).fetchone()
    if not club:
        flash('社团不存在', 'danger')
        return redirect(url_for('admin_dashboard'))

    name = request.form.get('name', '').strip()
    ctype = request.form.get('type')
    grade = request.form.get('grade') or None
    teacher = request.form.get('teacher', '').strip()
    location = request.form.get('location', '').strip()
    schedule = request.form.get('schedule', '').strip()
    description = request.form.get('description', '').strip()
    category = request.form.get('category', '').strip()
    max_students = request.form.get('max_students', MAX_STUDENTS_DEFAULT)

    if not name:
        flash('社团名称不能为空', 'danger')
        return redirect(url_for('admin_dashboard'))
    if ctype not in ('ordinary', 'premium'):
        ctype = club['type']
    if ctype == 'ordinary' and grade not in GRADES:
        flash('普通社团必须选择所属年级', 'danger')
        return redirect(url_for('admin_dashboard'))
    if ctype == 'premium':
        grade = None
    ok, max_students = validate_max_students(ctype, max_students)
    if not ok:
        if ctype == 'premium':
            flash('精品社团人数上限需在 10-30 之间', 'danger')
        else:
            flash('普通社团人数上限需在 15-32 之间', 'danger')
        return redirect(url_for('admin_dashboard'))
    if ctype == 'ordinary' and category not in CATEGORIES:
        flash('普通社团必须选择所属分类（体育/艺术/文化/科技/益智游戏）', 'danger')
        return redirect(url_for('admin_dashboard'))

    db.execute('''
        UPDATE clubs SET name=?, type=?, grade=?, category=?, teacher=?, location=?,
                         schedule=?, description=?, max_students=? WHERE id=?
    ''', (name, ctype, grade, category, teacher, location, schedule, description,
          max_students, cid))

    # 批量同步：勾选多个社团后，编辑其中任意一个保存 → 把本社团【被改动】的字段同步到其他勾选社团
    # （社团名称除外——名称通常唯一，不批量改名）
    apply_ids = [int(x) for x in request.form.getlist('apply_id') if x.isdigit()]
    if apply_ids:
        targets = [str(x) for x in apply_ids if x != cid]
        if targets:
            new_type = 'premium' if ctype == 'premium' else 'ordinary'
            new_grade = None if ctype == 'premium' else grade
            sets, params = [], []
            # 类型（普通/精品）变化 → 同步；变精品时一并清空年级
            if new_type != (club['type'] or 'ordinary'):
                sets.append('type=?'); params.append(new_type)
                if new_type == 'premium':
                    sets.append('grade=NULL')
            # 年级变化 → 同步（并确保为普通）
            if new_grade != club['grade']:
                if new_type == 'ordinary':
                    sets.append("type='ordinary'")
                sets.append('grade=?'); params.append(new_grade)
            # 分类（类别）变化 → 同步
            if category != (club['category'] or ''):
                sets.append('category=?'); params.append(category)
            if (teacher or '') != (club['teacher'] or ''):
                sets.append('teacher=?'); params.append(teacher or '')
            if (location or '') != (club['location'] or ''):
                sets.append('location=?'); params.append(location or '')
            if (schedule or '') != (club['schedule'] or ''):
                sets.append('schedule=?'); params.append(schedule or '')
            if (description or '') != (club['description'] or ''):
                sets.append('description=?'); params.append(description or '')
            try:
                new_max = int(max_students)
            except (TypeError, ValueError):
                new_max = MAX_STUDENTS_DEFAULT
            if new_max != club['max_students']:
                sets.append('max_students=?'); params.append(new_max)
            if sets:
                ph = ','.join('?' * len(targets))
                params.extend(targets)
                db.execute(f"UPDATE clubs SET {', '.join(sets)} WHERE id IN ({ph})", params)
                flash(f'已将改动同步应用到勾选的 {len(targets)} 个社团', 'info')
    db.commit()
    if apply_ids and len([x for x in apply_ids if x != cid]) > 0:
        flash(f'社团「{name}」内容已更新，并同步到勾选的社团', 'success')
    else:
        flash(f'社团「{name}」内容已更新', 'success')
    return redirect(url_for('admin_dashboard'))


# 管理员：批量修改社团内容（勾选多个社团，统一更新某项/多项内容）
@app.route('/admin/clubs/batch-edit', methods=['POST'])
def admin_clubs_batch_edit():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    ids = [i for i in request.form.getlist('cids') if i.isdigit()]
    if not ids:
        flash('请先勾选要修改的社团', 'warning')
        return redirect(url_for('admin_dashboard'))

    placeholders = ','.join('?' * len(ids))
    sets = []
    params = []
    # 仅更新填写了值的字段（留空表示不修改，避免误覆盖）
    field_map = [
        ('batch_type', 'type'),
        ('batch_grade', 'grade'),
        ('batch_category', 'category'),
        ('batch_teacher', 'teacher'),
        ('batch_location', 'location'),
        ('batch_schedule', 'schedule'),
    ]
    batch_type_val = request.form.get('batch_type', '').strip()
    batch_grade_val = request.form.get('batch_grade', '').strip()
    batch_category_val = request.form.get('batch_category', '').strip()
    for form_key, col in field_map:
        val = request.form.get(form_key, '').strip()
        if form_key == 'batch_type' and val not in ('ordinary', 'premium'):
            continue
        if form_key == 'batch_grade' and not val:
            continue
        if form_key == 'batch_category' and val not in CATEGORIES:
            continue
        # 批量设为精品时忽略年级（精品为全校，不设年级）
        if form_key == 'batch_grade' and batch_type_val == 'premium':
            continue
        # 批量设为普通时忽略年级同列的类型字段（由年级分支处理）
        if val:
            sets.append(f"{col}=?")
            params.append(val)
            if col == 'type' and val == 'premium':
                sets.append("grade=NULL")
            elif col == 'grade':
                sets.append("type='ordinary'")

    desc = request.form.get('batch_description', '').strip()
    if desc:
        sets.append("description=?")
        params.append(desc)

    if not sets:
        flash('未填写任何要批量修改的内容（至少填一项并勾选社团）', 'warning')
        return redirect(url_for('admin_dashboard'))

    params.extend(ids)
    db.execute(f"UPDATE clubs SET {', '.join(sets)} WHERE id IN ({placeholders})", params)
    db.commit()
    flash(f'已批量修改 {len(ids)} 个社团', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/club/delete/<int:cid>', methods=['POST'])
def admin_club_delete(cid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM registrations WHERE club_id=?", (cid,))
    db.execute("DELETE FROM clubs WHERE id=?", (cid,))
    db.commit()
    flash('社团已删除（其报名记录一并删除）', 'success')
    return redirect(url_for('admin_dashboard'))


# 社团 CSV 批量导入（名称,类型,年级,教师,地点,时间,人数上限,简介）
@app.route('/admin/clubs/import', methods=['POST'])
def admin_clubs_import():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    file = request.files.get('file')
    if not file or not file.filename:
        flash('请选择 CSV 文件', 'danger')
        return redirect(url_for('admin_dashboard'))

    raw = file.read()
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash('无法识别文件编码（支持 UTF-8 / GBK）', 'danger')
        return redirect(url_for('admin_dashboard'))

    created = 0
    errors = []
    try:
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if not row:
                continue
            # 兼容整行一个单元格 / 逗号分隔的多列
            parts = [p.strip() for p in (row[0].split(',') if len(row) == 1 else row)]
            name = parts[0] if len(parts) > 0 else ''
            if not name:
                continue
            # 跳过表头
            if name in ('社团名称', '社团', '名称', 'name', 'Name', '社团名'):
                continue
            ctype_raw = parts[1] if len(parts) > 1 else 'premium'
            ctype = 'ordinary' if ctype_raw in ('普通', '普通社团', 'ordinary', 'Ordinary') else 'premium'
            grade = parts[2] if len(parts) > 2 else ''
            if ctype == 'ordinary' and grade not in GRADES:
                errors.append(f'{name}: 普通社团必须指定所属年级（{"/".join(GRADES)}）')
                continue
            if ctype == 'premium':
                grade = None
            teacher = parts[3] if len(parts) > 3 else ''
            location = parts[4] if len(parts) > 4 else ''
            schedule = parts[5] if len(parts) > 5 else ''
            try:
                max_students = int(parts[6]) if len(parts) > 6 and parts[6] else MAX_STUDENTS_DEFAULT
                ok_ms, max_students = validate_max_students(ctype, max_students)
                if not ok_ms:
                    raise ValueError
            except ValueError:
                max_students = MAX_STUDENTS_DEFAULT
            description = parts[7] if len(parts) > 7 else ''
            # 第9列（index 8）：分类（普通社团可选，精品可不填）
            category = parts[8] if len(parts) > 8 and parts[8] in CATEGORIES else ''
            try:
                db.execute('''
                    INSERT INTO clubs (name, type, grade, category, description, teacher, location, schedule, max_students)
                    VALUES (?,?,?,?,?,?,?,?,?)
                ''', (name, ctype, grade, category, description, teacher, location, schedule, max_students))
                created += 1
            except sqlite3.IntegrityError:
                errors.append(f'{name}: 已存在，跳过')
    except Exception as e:
        flash(f'导入失败：{str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))

    db.commit()
    if created:
        flash(f'CSV 成功导入 {created} 个社团', 'success')
    for e in errors:
        flash(e, 'warning')
    if created == 0 and not errors:
        flash('CSV 中没有可导入的社团', 'warning')
    return redirect(url_for('admin_dashboard'))


# 社团 CSV 模板下载
@app.route('/admin/club-template')
def admin_club_template():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    data = ('社团名称,类型,所属年级,授课教师,上课地点,上课时间,人数上限,简介\n'
            '篮球社,普通,三年级,王老师,学校体育馆,周一、周三 16:30-17:30,30,校园篮球兴趣小组\n'
            '书法社,普通,四年级,李老师,美术教室,周二、周四 16:30-17:30,25,硬笔书法\n'
            '合唱团,精品,,张老师,音乐教室,周五 16:00-17:00,30,全校合唱\n').encode('utf-8-sig')
    resp = make_response(data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="club_template.csv"'
    return resp


# 班级账号批量创建（文本框粘贴，每行"班级名,年级"）
@app.route('/admin/classes', methods=['GET', 'POST'])
def admin_classes():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    if request.method == 'POST':
        lines = request.form.get('classes', '').strip().splitlines()
        created = 0
        errors = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 支持格式："班级名,年级" 或 "班级名"（自动从班级名解析年级），
            # 也可带任课教师/上课地点/上课时间："班级名,年级,教师,地点,时间"
            parts = [p.strip() for p in line.split(',')]
            cls_name = parts[0]
            grade = parts[1] if len(parts) > 1 else _guess_grade(cls_name)
            if not grade:
                errors.append(f'{cls_name}: 无法识别年级，请用 "班级名,年级" 格式')
                continue
            teacher = parts[2] if len(parts) > 2 else ''
            location = parts[3] if len(parts) > 3 else ''
            schedule = parts[4] if len(parts) > 4 else ''
            username = 'bj' + _pinyin_short(cls_name)
            password = '123456'
            try:
                db.execute('''
                    INSERT INTO class_accounts (class_name, grade, username, password, teacher, location, schedule)
                    VALUES (?,?,?,?,?,?,?)
                ''', (cls_name, grade, username, password, teacher, location, schedule))
                created += 1
            except sqlite3.IntegrityError:
                errors.append(f'{cls_name}: 已存在，跳过')
        db.commit()
        if created:
            flash(f'成功创建 {created} 个班级账号（默认密码 123456）', 'success')
        for e in errors:
            flash(e, 'warning')
        return redirect(url_for('admin_classes'))

    classes = db.execute('SELECT * FROM class_accounts ORDER BY id').fetchall()
    return render_template('admin_classes.html', classes=classes)


# 批量导入班级账号（CSV 上传）
@app.route('/admin/classes/import', methods=['POST'])
def admin_classes_import():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    file = request.files.get('file')
    if not file or not file.filename:
        flash('请选择 CSV 文件', 'danger')
        return redirect(url_for('admin_classes'))

    raw = file.read()
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash('无法识别文件编码（支持 UTF-8 / GBK）', 'danger')
        return redirect(url_for('admin_classes'))

    created = 0
    errors = []
    try:
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if not row:
                continue
            parts = [p.strip() for p in (row[0].split(',') if len(row) == 1 else row)]
            cls_name = parts[0]
            if not cls_name:
                continue
            # 跳过表头
            if cls_name in ('班级名', '班级', 'class', 'Class', '班级名称'):
                continue
            grade = parts[1] if len(parts) > 1 else _guess_grade(cls_name)
            if not grade:
                errors.append(f'{cls_name}: 无法识别年级，请用 "班级名,年级" 格式')
                continue
            # 可选列：任课教师/上课地点/上课时间（"班级名,年级,教师,地点,时间"）
            teacher = parts[2] if len(parts) > 2 else ''
            location = parts[3] if len(parts) > 3 else ''
            schedule = parts[4] if len(parts) > 4 else ''
            username = 'bj' + _pinyin_short(cls_name)
            password = '123456'
            try:
                db.execute('''
                    INSERT INTO class_accounts (class_name, grade, username, password, teacher, location, schedule)
                    VALUES (?,?,?,?,?,?,?)
                ''', (cls_name, grade, username, password, teacher, location, schedule))
                created += 1
            except sqlite3.IntegrityError:
                errors.append(f'{cls_name}: 已存在，跳过')
    except Exception as e:
        flash(f'导入失败：{str(e)}', 'danger')
        return redirect(url_for('admin_classes'))

    db.commit()
    if created:
        flash(f'CSV 成功导入 {created} 个班级账号（默认密码 123456）', 'success')
    for e in errors:
        flash(e, 'warning')
    if created == 0 and not errors:
        flash('CSV 中没有可导入的班级', 'warning')
    return redirect(url_for('admin_classes'))


# 班级 CSV 模板下载
@app.route('/admin/class-template')
def admin_class_template():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    data = ('班级名,年级,任课教师,上课地点,上课时间\n'
            '一年级1班,一年级,王老师,1号教学楼101,周一 16:30-17:30\n'
            '二年级1班,二年级,李老师,2号教学楼202,周二 16:30-17:30\n'
            '六年级3班,六年级,,,\n').encode('utf-8-sig')
    resp = make_response(data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="class_template.csv"'
    return resp


@app.route('/admin/class/delete/<int:cid>', methods=['POST'])
def admin_class_delete(cid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM registrations WHERE class_id=?", (cid,))
    db.execute("DELETE FROM students WHERE class_id=?", (cid,))
    db.execute("DELETE FROM class_accounts WHERE id=?", (cid,))
    db.commit()
    flash('班级账号已删除', 'success')
    return redirect(url_for('admin_classes'))


# 批量导入学生名单（CSV 上传）
@app.route('/admin/class/<int:cid>/import', methods=['POST'])
def admin_class_import(cid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    cls = db.execute("SELECT * FROM class_accounts WHERE id=?", (cid,)).fetchone()
    if not cls:
        flash('班级不存在', 'danger')
        return redirect(url_for('admin_classes'))

    file = request.files.get('file')
    if not file or not file.filename:
        flash('请选择 CSV 文件', 'danger')
        return redirect(url_for('admin_classes'))

    # 解析 CSV（处理 UTF-8 BOM 和 GBK 编码）
    raw = file.read()
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash('无法识别文件编码（支持 UTF-8 / GBK）', 'danger')
        return redirect(url_for('admin_classes'))

    imported = 0
    skipped = []
    try:
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if not row:
                continue
            name = (row[0] if row else '').strip()
            # 跳过表头
            if not name or name in ('姓名', '学生姓名', '学生', 'name', 'Name'):
                continue
            try:
                db.execute('''
                    INSERT INTO students (class_id, student_name) VALUES (?,?)
                ''', (cid, name))
                imported += 1
            except sqlite3.IntegrityError:
                skipped.append(name)
    except Exception as e:
        flash(f'导入失败：{str(e)}', 'danger')
        return redirect(url_for('admin_classes'))

    db.commit()
    msg = f'为「{cls["class_name"]}」成功导入 {imported} 名学生'
    if skipped:
        msg += f'，跳过重复 {len(skipped)} 名（{", ".join(skipped[:5])}...）'
    flash(msg, 'success')
    return redirect(url_for('admin_classes'))


# 学生名单 CSV 模板下载
@app.route('/admin/student-template')
def admin_student_template():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    data = '姓名\n张三\n李四\n王五\n'.encode('utf-8-sig')
    resp = make_response(data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="student_template.csv"'
    return resp


# 查看班级学生名单
@app.route('/admin/class/<int:cid>/students')
def admin_class_students(cid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    cls = db.execute("SELECT * FROM class_accounts WHERE id=?", (cid,)).fetchone()
    if not cls:
        flash('班级不存在', 'danger')
        return redirect(url_for('admin_classes'))
    students = db.execute('''
        SELECT s.*,
          (SELECT COUNT(*) FROM registrations r
           WHERE r.student_name=s.student_name AND r.class_id=s.class_id AND r.status != 'rejected') AS has_reg
        FROM students s WHERE s.class_id=? ORDER BY s.id
    ''', (cid,)).fetchall()
    return render_template('admin_class_students.html', cls=cls, students=students)


# 删除单个学生（从名单中移除）
@app.route('/admin/student/delete/<int:sid>', methods=['POST'])
def admin_student_delete(sid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    row = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if row:
        cid = row['class_id']
        db.execute("DELETE FROM students WHERE id=?", (sid,))
        db.commit()
        flash('已从名单移除该学生', 'success')
        return redirect(url_for('admin_class_students', cid=cid))
    return redirect(url_for('admin_classes'))


# ---------- 全校学生管理 ----------
# 全校学生名单（按班级分组列表）
@app.route('/admin/students')
def admin_students():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    classes = db.execute('SELECT * FROM class_accounts ORDER BY grade, id').fetchall()
    # 每个班级的学生数
    per_class = {}
    students = db.execute('''
        SELECT s.*, a.class_name, a.grade AS class_grade
        FROM students s JOIN class_accounts a ON s.class_id = a.id
        ORDER BY a.grade, a.id, s.id
    ''').fetchall()
    for s in students:
        per_class[s['class_id']] = per_class.get(s['class_id'], 0) + 1
    total = len(students)
    return render_template('admin_students.html', classes=classes,
                           students=students, per_class=per_class, total=total)


# 全校学生 CSV 导入（格式：班级,学生姓名，每行一个；也可 班级,姓名1,姓名2,...）
@app.route('/admin/students/import', methods=['POST'])
def admin_students_import():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    file = request.files.get('file')
    if not file or not file.filename:
        flash('请选择 CSV 文件', 'danger')
        return redirect(url_for('admin_students'))

    raw = file.read()
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash('无法识别文件编码（支持 UTF-8 / GBK）', 'danger')
        return redirect(url_for('admin_students'))

    imported = 0
    skipped = []
    notfound = []
    try:
        reader = csv.reader(io.StringIO(text))
        # 班级名 -> class_id 缓存
        class_map = {}
        for row in reader:
            if not row:
                continue
            parts = [p.strip() for p in (row[0].split(',') if len(row) == 1 else row)]
            if not parts or not parts[0]:
                continue
            cls_name = parts[0]
            # 跳过表头
            if cls_name in ('班级', '班级名', '班级名称', 'class', 'Class'):
                continue
            # 取该行所有剩余单元格作为学生姓名（兼容 班级,姓名 或 班级,姓名1,姓名2,...）
            names = parts[1:]
            if not names:
                continue
            if cls_name not in class_map:
                row_c = db.execute("SELECT id FROM class_accounts WHERE class_name=?", (cls_name,)).fetchone()
                class_map[cls_name] = row_c['id'] if row_c else None
            cid = class_map[cls_name]
            if not cid:
                notfound.append(cls_name)
                continue
            for nm in names:
                nm = nm.strip()
                if not nm or nm in ('姓名', '学生姓名', '学生', 'name', 'Name'):
                    continue
                try:
                    db.execute('INSERT INTO students (class_id, student_name) VALUES (?,?)', (cid, nm))
                    imported += 1
                except sqlite3.IntegrityError:
                    skipped.append(nm)
    except Exception as e:
        flash(f'导入失败：{str(e)}', 'danger')
        return redirect(url_for('admin_students'))

    db.commit()
    msg = f'成功导入 {imported} 名学生'
    if skipped:
        msg += f'，跳过重复 {len(skipped)} 名'
    if notfound:
        msg += f'，未找到班级 {len(notfound)} 个（{"、".join(notfound[:5])}）'
    flash(msg, 'success' if imported else 'warning')
    return redirect(url_for('admin_students'))


# 全校学生 CSV 模板下载
@app.route('/admin/students-template')
def admin_students_template():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    data = ('班级,学生姓名\n'
            '一年级1班,张三\n'
            '一年级1班,李四\n'
            '二年级1班,王五\n').encode('utf-8-sig')
    resp = make_response(data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="students_template.csv"'
    return resp


# 全校学生：从名单移除单个学生
@app.route('/admin/students/delete/<int:sid>', methods=['POST'])
def admin_students_delete(sid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    row = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if row:
        db.execute("DELETE FROM students WHERE id=?", (sid,))
        db.commit()
        flash('已从全校名单移除该学生', 'success')
    return redirect(url_for('admin_students'))


@app.route('/admin/registrations')
def admin_registrations():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    # 班级提交状态（看哪些班已提交合格、哪些还没提交）
    class_status = db.execute('''
        SELECT a.id AS class_id, a.class_name, a.grade,
               (s.submitted_at IS NOT NULL) AS submitted,
               s.submitted_at,
               (SELECT COUNT(*) FROM registrations r JOIN clubs c ON r.club_id=c.id
                 WHERE r.class_id=a.id AND r.status != 'rejected'
                       AND c.type='ordinary' AND c.grade=a.grade AND c.is_active=1) AS ord_cnt
        FROM class_accounts a
        LEFT JOIN class_submissions s ON s.class_id = a.id
        ORDER BY a.grade, a.id
    ''').fetchall()

    rows = db.execute('''
        SELECT r.*, c.name AS club_name, c.type AS club_type, c.grade AS club_grade,
               a.class_name,
               (SELECT count(*) FROM class_submissions cs WHERE cs.class_id = r.class_id) AS cls_submitted
        FROM registrations r
        JOIN clubs c ON r.club_id = c.id
        JOIN class_accounts a ON r.class_id = a.id
        ORDER BY r.id DESC
    ''').fetchall()
    return render_template('admin_registrations.html', rows=rows, MIN_STUDENTS=MIN_STUDENTS,
                           class_status=class_status)


@app.route('/admin/registration/status/<int:rid>/<status>', methods=['POST'])
def admin_reg_status(rid, status):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    if status not in ('pending', 'approved', 'rejected'):
        return redirect(url_for('admin_registrations'))
    db = get_db()
    db.execute("UPDATE registrations SET status=? WHERE id=?", (status, rid))
    db.commit()
    flash('状态已更新', 'success')
    return redirect(url_for('admin_registrations'))


# 批量审核报名（勾选多条 / 一键全审）
@app.route('/admin/registrations/batch', methods=['POST'])
def admin_registrations_batch():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    status = request.form.get('status')
    if status not in ('approved', 'rejected', 'pending'):
        flash('无效的审核操作', 'danger')
        return redirect(url_for('admin_registrations'))
    db = get_db()
    # 一键全审：不选具体 id 时对全部待审核记录操作
    ids = request.form.getlist('rids')
    if ids:
        ids = [i for i in ids if i.isdigit()]
        if not ids:
            flash('请先勾选要审核的报名记录', 'warning')
            return redirect(url_for('admin_registrations'))
        placeholders = ','.join('?' * len(ids))
        db.execute(f"UPDATE registrations SET status=? WHERE id IN ({placeholders})",
                   [status] + ids)
        flash(f'已批量更新 {len(ids)} 条报名记录', 'success')
    else:
        # 一键全审：所有待审核记录
        cur = db.execute("UPDATE registrations SET status=? WHERE status='pending'", (status,))
        n = cur.rowcount
        flash(f'已一键{("通过" if status=="approved" else "拒绝" if status=="rejected" else "设为待定")}全部 {n} 条待审核记录', 'success')
    db.commit()
    return redirect(url_for('admin_registrations'))


# ---------- 管理员：CSV 批量导入报名（班主任端/社团批量补录） ----------
# CSV 每行：班级名,学生姓名,社团名（首行可为表头：班级,学生,社团）
# 规则：全校查重（一个学生只能报一项，已报其它项则跳过提示）；
#       管理员导入不受"每班≤4"限制；导入后同步进该班学生名单。


def _find_club_for_import(db, club_cell, grade):
    """按社团名匹配。先精确匹配 clubs.name；匹配不到则视为项目名，按 年级+项目 匹配。"""
    club_cell = (club_cell or '').strip()
    if not club_cell:
        return None
    r = db.execute("SELECT id FROM clubs WHERE name=? AND is_active=1", (club_cell,)).fetchone()
    if r:
        return r['id']
    # 当作项目名：目标名 = 年级 + 项目（去掉可能的"年级"前缀再拼一次）
    cleaned = club_cell
    for g in GRADES:
        if cleaned.startswith(g):
            cleaned = cleaned[len(g):]
            break
    r = db.execute("SELECT id FROM clubs WHERE grade=? AND name=? AND type='ordinary' AND is_active=1",
                   (grade, f'{grade}{cleaned}')).fetchone()
    return r['id'] if r else None


@app.route('/admin/registrations/import', methods=['POST'])
def admin_registrations_import():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    file = request.files.get('file')
    if not file or not file.filename:
        flash('请选择 CSV 文件', 'danger')
        return redirect(url_for('admin_registrations'))
    raw = file.read()
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash('无法识别文件编码（支持 UTF-8 / GBK）', 'danger')
        return redirect(url_for('admin_registrations'))

    imported = 0
    skip_dup = 0
    skip_noclass = 0
    skip_noclub = 0
    skip_disabled = 0
    entries = []
    try:
        reader = csv.reader(io.StringIO(text))
        header_pos = {'class': 0, 'name': 1, 'club': 2}  # 默认：班级名,学生姓名,社团名
        first = True
        for row in reader:
            if not row:
                continue
            cols = [p for p in ((row[0].split(',') if len(row) == 1 else row))]
            cols = [c.strip() for c in cols]
            if first:
                first = False
                joined = ''.join(cols)
                # 表头识别（若首行含关键字）
                if any(k in joined for k in ('班级', '学生', '姓名', '社团', '项目', 'class', 'name', 'club')):
                    def _idx(kws, default):
                        for i, c in enumerate(cols[:6]):
                            if any(k == c or k in c for k in kws):
                                return i
                        return default
                    header_pos = {
                        'class': _idx(['班级名', '班级', 'class', 'Class'], 0),
                        'name': _idx(['学生姓名', '姓名', '学生', '名字', 'name'], 1),
                        'club': _idx(['社团名', '社团', '项目', 'club', 'course'], 2),
                    }
                    continue
            data = cols  # 无表头：直接按顺序, 班级名,学生姓名,社团名
            cls_cell = (data[header_pos['class']] if len(data) > header_pos['class'] else '')
            name_cell = (data[header_pos['name']] if len(data) > header_pos['name'] else '')
            club_cell = (data[header_pos['club']] if len(data) > header_pos['club'] else '')
            if not (cls_cell and name_cell and club_cell):
                continue
            entries.append((cls_cell, name_cell, club_cell))

        # 预取：班级按班名、已报学生集合（全校一个学生只能报一项）
        class_by_name = {r['class_name']: r for r in
                         db.execute("SELECT id, class_name, grade FROM class_accounts").fetchall()}
        for cls_cell, name_cell, club_cell in entries:
            clazz = class_by_name.get(cls_cell)
            if not clazz:
                skip_noclass += 1
                continue
            class_id = clazz['id']
            cid = _find_club_for_import(db, club_cell, clazz['grade'])
            if not cid:
                skip_noclub += 1
                continue
            club = db.execute("SELECT * FROM clubs WHERE id=? AND is_active=1", (cid,)).fetchone()
            if not club:
                skip_disabled += 1
                continue
            # 该生是否已报其它项目（全校查重）
            already = db.execute('''
                SELECT COUNT(*) FROM registrations
                WHERE student_name=? AND class_id=? AND status != 'rejected'
            ''', (name_cell, class_id)).fetchone()[0]
            if already:
                skip_dup += 1
                continue
            # 避免文件内/库里同校同生重复（同班同名同一项目）
            dup2 = db.execute('''
                SELECT COUNT(*) FROM registrations
                WHERE club_id=? AND class_id=? AND student_name=? AND status != 'rejected'
            ''', (cid, class_id, name_cell)).fetchone()[0]
            if dup2:
                skip_dup += 1
                continue
            db.execute('''
                INSERT INTO registrations (club_id, class_id, student_name, grade, status, created_at)
                VALUES (?,?,?,?, 'pending', datetime('now','localtime'))
            ''', (cid, class_id, name_cell, clazz['grade']))
            # 同步进该班学生名单（若无则补录）
            db.execute('INSERT OR IGNORE INTO students (class_id, student_name) VALUES (?,?)',
                       (class_id, name_cell))
            imported += 1
    except Exception as e:
        db.rollback()
        flash(f'导入失败：{str(e)}', 'danger')
        return redirect(url_for('admin_registrations'))

    db.commit()
    if imported:
        flash(f'CSV 成功导入 {imported} 条报名记录（已同步进对应班级名单）', 'success')
    if skip_dup:
        flash(f'{skip_dup} 条因学生已报其它项目(一人只能报一项)被跳过', 'warning')
    if skip_noclass:
        flash(f'{skip_noclass} 条因班级不存在被跳过（请检查班级名）', 'warning')
    if skip_noclub:
        flash(f'{skip_noclub} 条因社团未找到被跳过', 'warning')
    if skip_disabled:
        flash(f'{skip_disabled} 条因社团已停用被跳过', 'warning')
    if imported == 0 and not (skip_dup or skip_noclass or skip_noclub or skip_disabled):
        flash('CSV 中没有可导入的记录', 'warning')
    return redirect(url_for('admin_registrations'))


@app.route('/admin/registrations/template')
def admin_registrations_template():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    data = ('班级名,学生姓名,社团名\n'
            '三年级1班,张三,三年级篮球\n'
            '三年级1班,李四,三年级足球\n'
            '一年级5班,王五,一年级跳绳\n').encode('utf-8-sig')
    resp = make_response(data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="registrations_import_template.csv"'
    return resp


# ---------- 管理员：教师账号管理 ----------
# 教师账号管理页（含创建、列表、删除）
@app.route('/admin/teachers')
def admin_teachers():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    teachers = db.execute('''
        SELECT t.*,
          (SELECT COUNT(*) FROM teacher_clubs tc WHERE tc.teacher_id=t.id) AS club_count
        FROM teachers t ORDER BY t.id
    ''').fetchall()
    # 每个教师任教的社团名
    teacher_clubs_map = {}
    for t in teachers:
        clubs = db.execute('''
            SELECT c.name, c.type FROM teacher_clubs tc JOIN clubs c ON tc.club_id=c.id
            WHERE tc.teacher_id=? ORDER BY c.id
        ''', (t['id'],)).fetchall()
        teacher_clubs_map[t['id']] = clubs
    return render_template('admin_teachers.html', teachers=teachers,
                           teacher_clubs_map=teacher_clubs_map)


# 创建教师账号
@app.route('/admin/teacher/create', methods=['POST'])
def admin_teacher_create():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    name = request.form.get('name', '').strip()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip() or '123456'
    subjects = request.form.get('subjects', '').strip()
    db = get_db()
    if not name or not username:
        flash('请填写教师姓名和账号', 'danger')
        return redirect(url_for('admin_teachers'))
    try:
        db.execute('''
            INSERT INTO teachers (name, username, password, subjects)
            VALUES (?,?,?,?)
        ''', (name, username, password, subjects))
        db.commit()
        flash(f'教师账号「{name}」创建成功（账号 {username}）', 'success')
    except sqlite3.IntegrityError:
        flash(f'账号 {username} 已存在', 'danger')
    return redirect(url_for('admin_teachers'))


# 批量创建教师账号（文本框，每行"姓名,账号[,密码][,擅长学科]"）
@app.route('/admin/teachers/batch', methods=['POST'])
def admin_teachers_batch():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    lines = request.form.get('teachers', '').strip().splitlines()
    db = get_db()
    created = 0
    errors = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(',')]
        name = parts[0]
        username = parts[1] if len(parts) > 1 else ''
        password = parts[2] if len(parts) > 2 else '123456'
        subjects = parts[3] if len(parts) > 3 else ''
        if not name or not username:
            errors.append(f'{line}: 需至少"姓名,账号"')
            continue
        try:
            db.execute('INSERT INTO teachers (name, username, password, subjects) VALUES (?,?,?,?)',
                       (name, username, password, subjects))
            created += 1
        except sqlite3.IntegrityError:
            errors.append(f'{username}: 账号已存在')
    db.commit()
    if created:
        flash(f'成功创建 {created} 个教师账号（默认密码 123456）', 'success')
    for e in errors:
        flash(e, 'warning')
    return redirect(url_for('admin_teachers'))


# CSV 批量导入教师账号（姓名,账号,密码,擅长学科）
@app.route('/admin/teachers/import', methods=['POST'])
def admin_teachers_import():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    file = request.files.get('file')
    if not file or not file.filename:
        flash('请选择 CSV 文件', 'danger')
        return redirect(url_for('admin_teachers'))

    raw = file.read()
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash('无法识别文件编码（支持 UTF-8 / GBK）', 'danger')
        return redirect(url_for('admin_teachers'))

    created = 0
    errors = []
    try:
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if not row:
                continue
            # 兼容整行一个单元格 / 逗号分隔的多列
            parts = [p.strip() for p in (row[0].split(',') if len(row) == 1 else row)]
            name = parts[0] if len(parts) > 0 else ''
            if not name:
                continue
            # 跳过表头
            if name in ('姓名', '教师姓名', 'name', 'Name'):
                continue
            username = parts[1] if len(parts) > 1 else ''
            password = parts[2] if len(parts) > 2 and parts[2] else '123456'
            subjects = parts[3] if len(parts) > 3 else ''
            if not username:
                errors.append(f'{name}: 缺少登录账号')
                continue
            try:
                db.execute('INSERT INTO teachers (name, username, password, subjects) VALUES (?,?,?,?)',
                           (name, username, password, subjects))
                created += 1
            except sqlite3.IntegrityError:
                errors.append(f'{username}: 账号已存在')
    except Exception as e:
        flash(f'导入失败：{str(e)}', 'danger')
        return redirect(url_for('admin_teachers'))

    db.commit()
    if created:
        flash(f'CSV 成功导入 {created} 个教师账号（默认密码 123456）', 'success')
    for e in errors:
        flash(e, 'warning')
    if created == 0 and not errors:
        flash('CSV 中没有可导入的教师账号', 'warning')
    return redirect(url_for('admin_teachers'))


# 教师 CSV 模板下载
@app.route('/admin/teachers-template')
def admin_teachers_template():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['姓名', '账号', '密码', '擅长学科'])
    writer.writerow(['王老师', 'wang', '123456', '语文,书法'])
    writer.writerow(['李老师', 'li', '123456', '数学'])
    data = buf.getvalue().encode('utf-8-sig')
    resp = make_response(data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="teachers_template.csv"'
    return resp


# 删除教师账号
@app.route('/admin/teacher/delete/<int:tid>', methods=['POST'])
def admin_teacher_delete(tid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    # 解除其任教关联
    db.execute("DELETE FROM teacher_clubs WHERE teacher_id=?", (tid,))
    db.execute("DELETE FROM teachers WHERE id=?", (tid,))
    db.commit()
    flash('教师账号已删除', 'success')
    return redirect(url_for('admin_teachers'))


# ---------- 管理员：巡课人员账号管理 ----------
# 巡课人员账号管理页（创建/列表/删除）
@app.route('/admin/inspectors')
def admin_inspectors():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    inspectors = db.execute("SELECT * FROM inspectors ORDER BY id").fetchall()
    return render_template('admin_inspectors.html', inspectors=inspectors)


# 创建巡课人员账号
@app.route('/admin/inspector/create', methods=['POST'])
def admin_inspector_create():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    name = request.form.get('name', '').strip()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip() or '123456'
    db = get_db()
    if not name or not username:
        flash('请填写巡课人姓名和账号', 'danger')
        return redirect(url_for('admin_inspectors'))
    try:
        db.execute('INSERT INTO inspectors (name, username, password) VALUES (?,?,?)',
                   (name, username, password))
        db.commit()
        flash(f'巡课账号「{name}」创建成功（账号 {username}）', 'success')
    except sqlite3.IntegrityError:
        flash(f'账号 {username} 已存在', 'danger')
    return redirect(url_for('admin_inspectors'))


# 删除巡课人员账号
@app.route('/admin/inspector/delete/<int:iid>', methods=['POST'])
def admin_inspector_delete(iid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM inspectors WHERE id=?", (iid,))
    db.commit()
    flash('巡课账号已删除', 'success')
    return redirect(url_for('admin_inspectors'))


# ---------- 管理员：巡课记录查看 ----------
# 巡课记录总览（按日期筛选，查看哪一天哪些人在哪里上课 + 评价反馈）
@app.route('/admin/inspections')
def admin_inspections():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    # 日期筛选（默认全部）
    date = request.args.get('date', '')
    if date:
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            date = ''
    if date:
        rows = db.execute('''
            SELECT i.*, c.name AS club_name, c.type AS club_type, c.grade AS club_grade
            FROM inspections i JOIN clubs c ON i.club_id=c.id
            WHERE i.inspection_date=? ORDER BY i.inspection_date DESC, i.id DESC
        ''', (date,)).fetchall()
    else:
        rows = db.execute('''
            SELECT i.*, c.name AS club_name, c.type AS club_type, c.grade AS club_grade
            FROM inspections i JOIN clubs c ON i.club_id=c.id
            ORDER BY i.inspection_date DESC, i.id DESC
        ''').fetchall()
    # 所有巡课日期（用于下拉筛选）
    dates = [r[0] for r in db.execute(
        "SELECT DISTINCT inspection_date FROM inspections ORDER BY inspection_date DESC").fetchall()]
    return render_template('admin_inspections.html', rows=rows, date=date, dates=dates)


# 导出巡课记录 CSV
@app.route('/admin/inspections/export')
def admin_inspections_export():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    rows = db.execute('''
        SELECT i.*, c.name AS club_name, c.type AS club_type, c.grade AS club_grade
        FROM inspections i JOIN clubs c ON i.club_id=c.id
        ORDER BY i.inspection_date DESC, i.id DESC
    ''').fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['巡课日期', '社团名称', '社团类型', '授课教师', '上课地点', '上课时间', '巡课人', '评分(1-5)', '评价反馈'])
    for r in rows:
        writer.writerow([r['inspection_date'], r['club_name'],
                         '精品' if r['club_type'] == 'premium' else '普通',
                         r['teacher'] or '', r['location'] or '', r['schedule'] or '',
                         r['inspector'], r['rating'], r['comment']])
    data = buf.getvalue().encode('utf-8-sig')
    resp = make_response(data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="inspections.csv"'
    return resp


# 删除单条巡课记录
@app.route('/admin/inspection/delete/<int:iid>', methods=['POST'])
def admin_inspection_delete(iid):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM inspections WHERE id=?", (iid,))
    db.commit()
    flash('巡课记录已删除', 'success')
    return redirect(url_for('admin_inspections'))


# 导出 CSV
@app.route('/admin/export')
def admin_export():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    rows = db.execute('''
        SELECT a.class_name, r.grade, r.student_name,
               c.name AS club_name, c.type AS club_type,
               CASE WHEN c.type='premium' THEN '全校' ELSE c.grade END AS club_grade,
               r.status, r.created_at
        FROM registrations r
        JOIN clubs c ON r.club_id = c.id
        JOIN class_accounts a ON r.class_id = a.id
        ORDER BY r.grade, a.class_name, c.name
    ''').fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['班级', '学生年级', '学生姓名', '社团名称', '社团类型', '社团年级', '状态', '报名时间'])
    for r in rows:
        writer.writerow([r['class_name'], r['grade'], r['student_name'],
                         r['club_name'], '精品' if r['club_type'] == 'premium' else '普通',
                         r['club_grade'], r['status'], r['created_at']])

    output = buf.getvalue()
    data = output.encode('utf-8-sig')
    resp = make_response(data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="afterschool_registrations.csv"'
    return resp


# ---------- 工具：从班级名猜测年级 ----------
def _guess_grade(cls_name):
    for g in GRADES:
        if g in cls_name:
            return g
    return None


def _pinyin_short(cls_name):
    # 简化：用拼音首字母数字标识，避免依赖第三方库
    mapping = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6'}
    out = []
    for ch in cls_name:
        if ch in mapping:
            out.append(mapping[ch])
        elif ch.isdigit():
            out.append(ch)
    return ''.join(out) if out else 'cls'


init_db()


# ---------- 全局错误日志（定位线上 500） ----------
@app.errorhandler(Exception)
def handle_exception(e):
    """记录任何未捕获异常到持久卷，便于定位线上问题（生产不显示 traceback）"""
    try:
        tb = traceback.format_exc()
        log_path = os.path.join(DATA_DIR, 'error.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====\n{tb}\n")
    except Exception:
        pass
    # 记录后返回标准 500 页面
    return 'Internal Server Error', 500


# 调试端点：管理员可查看最近错误日志（排查用）
@app.route('/admin/debug-log')
def admin_debug_log():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    log_path = os.path.join(DATA_DIR, 'error.log')
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        content = '（暂无错误日志）'
    except Exception as ex:
        content = f'读取日志失败: {ex}'
    return '<pre style="font-size:12px;white-space:pre-wrap">' + content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') + '</pre>'


# ---------- PWA（手机端安装支持） ----------
@app.route('/manifest.json')
def pwa_manifest():
    from flask import send_from_directory
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')


@app.route('/sw.js')
def pwa_sw():
    from flask import send_from_directory
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
