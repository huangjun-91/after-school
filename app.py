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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
