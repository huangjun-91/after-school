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
MAX_STUDENTS_DEFAULT = 30
MIN_STUDENTS = 15


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
                         ('schedule', 'TEXT DEFAULT \'\'')]:
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
        SELECT r.*, c.name AS club_name, c.type AS club_type
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

    # 每个社团的报名人数与状态
    club_info = {}
    for c in clubs:
        cnt = count_registrations(db, c['id'])
        club_info[c['id']] = {
            'count': cnt,
            'full': cnt >= c['max_students'],
        }

    # 本班学生名单（管理员批量导入的）
    students = db.execute('''
        SELECT s.*,
          (SELECT COUNT(*) FROM registrations r
           WHERE r.student_name=s.student_name AND r.class_id=s.class_id AND r.status != 'rejected') AS has_reg
        FROM students s WHERE s.class_id=? ORDER BY s.id
    ''', (class_id,)).fetchall()

    # 本班任课教师/上课地点/上课时间（由 CSV 导入时填写）
    cls = db.execute("SELECT * FROM class_accounts WHERE id=?", (class_id,)).fetchone()

    return render_template('teacher.html', regs=regs, clubs=clubs,
                           club_info=club_info, grade=grade,
                           class_name=session['class_name'], students=students,
                           cls=cls)


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

    # 人数上限检查
    cnt = count_registrations(db, club['id'])
    if cnt >= club['max_students']:
        flash(f'该社团已满员（{club["max_students"]}人），不能再报名', 'danger')
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

    # 可选的社团：未任教 + 启用；若设置了擅长学科，优先展示匹配的
    available = db.execute('''
        SELECT c.*,
          (SELECT COUNT(*) FROM registrations r
           WHERE r.club_id=c.id AND r.status IN ('pending','approved')) AS cnt
        FROM clubs c WHERE c.is_active=1
          AND c.id NOT IN (SELECT club_id FROM teacher_clubs WHERE teacher_id=?)
        ORDER BY c.type DESC, c.id
    ''', (teacher_id,)).fetchall()

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

    # 所有教师（用于后台管理展示，教师端不需要）
    return render_template('teacher_home.html', teacher=teacher, subjects=subjects,
                           my_clubs=my_clubs, my_club_students=my_club_students,
                           available=available, recommended_ids=recommended_ids,
                           GRADES=GRADES, my_club_count=len(my_clubs))


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
    # 最多任教 2 门
    cnt = db.execute("SELECT COUNT(*) FROM teacher_clubs WHERE teacher_id=?",
                     (teacher_id,)).fetchone()[0]
    if cnt >= 2:
        flash('每位教师最多只能任教 2 门课程。如需调整，请先在右侧退出已任教的课程。', 'warning')
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
    try:
        max_students = int(max_students)
        if not (15 <= max_students <= 30):
            raise ValueError
    except ValueError:
        max_students = MAX_STUDENTS_DEFAULT

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

    # 当日排课：有上课地点/时间的启用社团；未排课地点时间的也列出（可巡课）
    day_clubs = db.execute('''
        SELECT c.*,
          (SELECT COUNT(*) FROM registrations r
           WHERE r.club_id=c.id AND r.status IN ('pending','approved')) AS cnt
        FROM clubs c WHERE c.is_active=1
        ORDER BY c.type DESC, c.id
    ''').fetchall()

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
                           total_clubs=total_clubs, inspected=inspected)


@app.route('/inspector/submit', methods=['POST'])
def inspector_submit():
    """提交巡课评价反馈（可同时修改该社团的任课教师）"""
    if not session.get('role') == 'inspector':
        return redirect(url_for('login'))
    db = get_db()
    inspection_date = request.form.get('inspection_date', '').strip()
    club_id = request.form.get('club_id')
    rating = request.form.get('rating', '3')
    comment = request.form.get('comment', '').strip()
    new_teacher = request.form.get('teacher', '').strip()   # 可修改任课教师
    inspector_name = session.get('inspector_name', '巡课人员')

    try:
        datetime.strptime(inspection_date, '%Y-%m-%d')
    except ValueError:
        flash('日期格式错误', 'danger')
        return redirect(url_for('inspector_dashboard'))

    club = db.execute("SELECT * FROM clubs WHERE id=?", (club_id,)).fetchone()
    if not club:
        flash('社团不存在', 'danger')
        return redirect(url_for('inspector_dashboard'))

    try:
        rating = int(rating)
        if not (1 <= rating <= 5):
            raise ValueError
    except ValueError:
        flash('评分需在 1-5 星之间', 'danger')
        return redirect(url_for('inspector_dashboard'))

    # 若巡课人填写了任课教师且与当前不同，则同步更新社团的授课教师字段
    teacher_saved = club['teacher'] or ''
    if new_teacher and new_teacher != (club['teacher'] or ''):
        db.execute("UPDATE clubs SET teacher=? WHERE id=?", (new_teacher, club_id))
        teacher_saved = new_teacher
        flash('已同步更新任课教师', 'success')

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
        flash('巡课记录已更新', 'success')
    else:
        db.execute('''
            INSERT INTO inspections
            (inspection_date, club_id, inspector, location, teacher, schedule, rating, comment)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (inspection_date, club_id, inspector_name,
              club['location'] or '', teacher_saved, club['schedule'] or '',
              rating, comment))
        flash('巡课评价已提交', 'success')
    db.commit()
    return redirect(url_for('inspector_dashboard', date=inspection_date))


# ---------- 后台（管理员） ----------
@app.route('/admin')
def admin_dashboard():
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    clubs = db.execute('''
        SELECT c.*,
          (SELECT COUNT(*) FROM registrations r WHERE r.club_id=c.id AND r.status IN ('pending','approved')) AS cnt
        FROM clubs c ORDER BY c.type DESC, c.id
    ''').fetchall()
    low_count = [c for c in clubs if c['cnt'] < MIN_STUDENTS]
    return render_template('admin.html', clubs=clubs, low_count=low_count,
                           MIN_STUDENTS=MIN_STUDENTS, GRADES=GRADES)


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
    db = get_db()

    if not name or ctype not in ('ordinary', 'premium'):
        flash('请填写社团名称并选择类型', 'danger')
        return redirect(url_for('admin_dashboard'))
    if ctype == 'ordinary' and grade not in GRADES:
        flash('普通社团必须选择所属年级', 'danger')
        return redirect(url_for('admin_dashboard'))
    try:
        max_students = int(max_students)
        if not (15 <= max_students <= 30):
            raise ValueError
    except ValueError:
        flash('人数上限需在 15-30 之间', 'danger')
        return redirect(url_for('admin_dashboard'))

    db.execute('''
        INSERT INTO clubs (name, type, grade, description, teacher, location, schedule, max_students)
        VALUES (?,?,?,?,?,?,?,?)
    ''', (name, ctype, grade, description, teacher, location, schedule, max_students))
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
                if not (15 <= max_students <= 30):
                    raise ValueError
            except ValueError:
                max_students = MAX_STUDENTS_DEFAULT
            description = parts[7] if len(parts) > 7 else ''
            try:
                db.execute('''
                    INSERT INTO clubs (name, type, grade, description, teacher, location, schedule, max_students)
                    VALUES (?,?,?,?,?,?,?,?)
                ''', (name, ctype, grade, description, teacher, location, schedule, max_students))
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
    rows = db.execute('''
        SELECT r.*, c.name AS club_name, c.type AS club_type, c.grade AS club_grade,
               a.class_name
        FROM registrations r
        JOIN clubs c ON r.club_id = c.id
        JOIN class_accounts a ON r.class_id = a.id
        ORDER BY r.id DESC
    ''').fetchall()
    return render_template('admin_registrations.html', rows=rows, MIN_STUDENTS=MIN_STUDENTS)


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


# 不足15人的社团处理：管理员决定继续/关闭
@app.route('/admin/lowcount/<int:cid>/<action>', methods=['POST'])
def admin_lowcount(cid, action):
    if not session.get('role') == 'admin':
        return redirect(url_for('login'))
    db = get_db()
    if action == 'keep':
        # 标记为保留（不关闭），仅提示
        flash('已确认保留该社团（报名继续开放）', 'success')
    elif action == 'close':
        db.execute("UPDATE clubs SET is_active=0 WHERE id=?", (cid,))
        db.commit()
        flash('该社团已关闭（学生需重新选择）', 'success')
    return redirect(url_for('admin_dashboard'))


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
