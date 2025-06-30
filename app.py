import os
import re
import json
import smtplib
import sqlite3
import PyPDF2
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from werkzeug.utils import secure_filename
from docx import Document
import nltk
from collections import defaultdict

# Download required NLTK data
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    print("Downloading NLTK data...")
    nltk.download('punkt', quiet=True)
    print("NLTK data downloaded successfully!")

# Flask application configuration
app = Flask(__name__)
app.secret_key = 'hr-analyzer-secret-key-2024'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Create uploads directory
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def init_db():
    """Initialize SQLite database with required tables"""
    print("Initializing database...")
    conn = sqlite3.connect('hr_analyzer.db')
    cursor = conn.cursor()
    
    # Create candidates table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            resume_path TEXT NOT NULL,
            total_score REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create scoring rules table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scoring_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name TEXT NOT NULL,
            keywords TEXT NOT NULL,
            weightage INTEGER NOT NULL,
            rule_type TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create job postings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS job_postings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            candidate_limit INTEGER DEFAULT 10,
            email_template TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create candidate scores table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS candidate_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            rule_id INTEGER,
            score REAL,
            matched_keywords TEXT,
            FOREIGN KEY (candidate_id) REFERENCES candidates (id),
            FOREIGN KEY (rule_id) REFERENCES scoring_rules (id)
        )
    ''')
    
    conn.commit()
    conn.close()
    print("Database initialized successfully!")

# Resume Parser Class
class ResumeParser:
    def __init__(self):
        self.experience_patterns = [
            r'(\d+)\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)',
            r'experience\s*[:]\s*(\d+)\s*(?:years?|yrs?)',
            r'(\d+)\+?\s*(?:years?|yrs?)'
        ]
        
        self.email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        self.phone_pattern = r'(?:\+?1[-.\s]?)?\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}'
    
    def extract_text_from_pdf(self, file_path):
        try:
            with open(file_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                text = ""
                for page in pdf_reader.pages:
                    text += page.extract_text()
                return text
        except Exception as e:
            print(f"Error reading PDF: {e}")
            return ""
    
    def extract_text_from_docx(self, file_path):
        try:
            doc = Document(file_path)
            text = ""
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
            return text
        except Exception as e:
            print(f"Error reading DOCX: {e}")
            return ""
    
    def extract_text_from_txt(self, file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                return file.read()
        except Exception as e:
            print(f"Error reading TXT: {e}")
            return ""
    
    def parse_resume(self, file_path):
        # Determine file type and extract text
        file_extension = os.path.splitext(file_path)[1].lower()
        
        if file_extension == '.pdf':
            text = self.extract_text_from_pdf(file_path)
        elif file_extension == '.docx':
            text = self.extract_text_from_docx(file_path)
        elif file_extension == '.txt':
            text = self.extract_text_from_txt(file_path)
        else:
            return None
        
        if not text:
            return None
        
        # Extract information
        parsed_data = {
            'raw_text': text,
            'email': self.extract_email(text),
            'phone': self.extract_phone(text),
            'experience_years': self.extract_experience_years(text),
            'skills': self.extract_skills(text),
            'education': self.extract_education(text)
        }
        
        return parsed_data
    
    def extract_email(self, text):
        matches = re.findall(self.email_pattern, text)
        return matches[0] if matches else ""
    
    def extract_phone(self, text):
        matches = re.findall(self.phone_pattern, text)
        return matches[0] if matches else ""
    
    def extract_experience_years(self, text):
        text_lower = text.lower()
        for pattern in self.experience_patterns:
            matches = re.findall(pattern, text_lower)
            if matches:
                return int(matches[0])
        return 0
    
    def extract_skills(self, text):
        # Common technical skills
        common_skills = [
            'python', 'java', 'javascript', 'react', 'angular', 'vue', 'node.js',
            'html', 'css', 'sql', 'mongodb', 'postgresql', 'mysql', 'aws',
            'azure', 'docker', 'kubernetes', 'git', 'linux', 'windows',
            'machine learning', 'ai', 'data science', 'tensorflow', 'pytorch',
            'php', 'c++', 'c#', '.net', 'ruby', 'go', 'rust', 'swift'
        ]
        
        text_lower = text.lower()
        found_skills = []
        
        for skill in common_skills:
            if skill in text_lower:
                found_skills.append(skill)
        
        return found_skills
    
    def extract_education(self, text):
        education_keywords = ['bachelor', 'master', 'phd', 'degree', 'university', 'college']
        text_lower = text.lower()
        
        for keyword in education_keywords:
            if keyword in text_lower:
                return keyword
        
        return ""

# Scoring Engine
class ScoringEngine:
    def __init__(self):
        self.parser = ResumeParser()
    
    def calculate_score(self, candidate_id, parsed_resume):
        conn = sqlite3.connect('hr_analyzer.db')
        cursor = conn.cursor()
        
        # Get all scoring rules
        cursor.execute('SELECT * FROM scoring_rules')
        rules = cursor.fetchall()
        
        total_score = 0
        
        for rule in rules:
            rule_id, rule_name, keywords, weightage, rule_type, created_at = rule
            keywords_list = json.loads(keywords)
            
            rule_score = 0
            matched_keywords = []
            
            if rule_type == 'experience':
                years = parsed_resume.get('experience_years', 0)
                if years >= int(keywords_list[0]):  # Minimum years required
                    rule_score = weightage
                    matched_keywords = [f"{years} years"]
            
            elif rule_type == 'skills':
                resume_skills = [skill.lower() for skill in parsed_resume.get('skills', [])]
                for keyword in keywords_list:
                    if keyword.lower() in resume_skills or keyword.lower() in parsed_resume['raw_text'].lower():
                        rule_score += weightage / len(keywords_list)
                        matched_keywords.append(keyword)
            
            elif rule_type == 'education':
                education = parsed_resume.get('education', '').lower()
                for keyword in keywords_list:
                    if keyword.lower() in education:
                        rule_score = weightage
                        matched_keywords.append(keyword)
                        break
            
            elif rule_type == 'general':
                text = parsed_resume['raw_text'].lower()
                for keyword in keywords_list:
                    if keyword.lower() in text:
                        rule_score += weightage / len(keywords_list)
                        matched_keywords.append(keyword)
            
            # Save individual rule score
            cursor.execute('''
                INSERT INTO candidate_scores (candidate_id, rule_id, score, matched_keywords)
                VALUES (?, ?, ?, ?)
            ''', (candidate_id, rule_id, rule_score, json.dumps(matched_keywords)))
            
            total_score += rule_score
        
        # Update candidate total score
        cursor.execute('''
            UPDATE candidates SET total_score = ? WHERE id = ?
        ''', (total_score, candidate_id))
        
        conn.commit()
        conn.close()
        
        return total_score

# Email Service
class EmailService:
    def __init__(self):
        # Configure your email settings here
        self.smtp_server = "smtp.gmail.com"
        self.smtp_port = 587
        self.email = "your-email@gmail.com"  # Replace with your email
        self.password = "your-app-password"  # Replace with your app password
    
    def send_interview_invitation(self, candidate_email, candidate_name, template):
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email
            msg['To'] = candidate_email
            msg['Subject'] = "Interview Invitation"
            
            # Personalize template
            body = template.replace("{candidate_name}", candidate_name)
            msg.attach(MIMEText(body, 'plain'))
            
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.email, self.password)
            text = msg.as_string()
            server.sendmail(self.email, candidate_email, text)
            server.quit()
            
            return True
        except Exception as e:
            print(f"Email sending failed: {e}")
            return False

# Initialize components
parser = ResumeParser()
scoring_engine = ScoringEngine()
email_service = EmailService()

# Flask Routes
@app.route('/')
def index():
    return render_template('base.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload_resume():
    if request.method == 'POST':
        if 'resume' not in request.files:
            flash('No file selected')
            return redirect(request.url)
        
        file = request.files['resume']
        name = request.form['name']
        email = request.form['email']
        phone = request.form.get('phone', '')
        
        if file.filename == '':
            flash('No file selected')
            return redirect(request.url)
        
        if file and file.filename.lower().endswith(('.pdf', '.docx', '.txt')):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            
            # Parse resume
            parsed_resume = parser.parse_resume(file_path)
            
            if parsed_resume:
                # Save candidate to database
                conn = sqlite3.connect('hr_analyzer.db')
                cursor = conn.cursor()
                
                cursor.execute('''
                    INSERT INTO candidates (name, email, phone, resume_path)
                    VALUES (?, ?, ?, ?)
                ''', (name, email or parsed_resume['email'], phone or parsed_resume['phone'], file_path))
                
                candidate_id = cursor.lastrowid
                conn.commit()
                conn.close()
                
                # Calculate score
                score = scoring_engine.calculate_score(candidate_id, parsed_resume)
                
                flash(f'Resume uploaded successfully! Score: {score:.2f}')
                return redirect(url_for('candidates'))
            else:
                flash('Error parsing resume')
                return redirect(request.url)
        else:
            flash('Invalid file type. Please upload PDF, DOCX, or TXT files.')
            return redirect(request.url)
    
    return render_template('upload.html')

@app.route('/scoring-rules', methods=['GET', 'POST'])
def scoring_rules():
    if request.method == 'POST':
        rule_name = request.form['rule_name']
        keywords = request.form['keywords'].split(',')
        keywords = [k.strip() for k in keywords]
        weightage = int(request.form['weightage'])
        rule_type = request.form['rule_type']
        
        conn = sqlite3.connect('hr_analyzer.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO scoring_rules (rule_name, keywords, weightage, rule_type)
            VALUES (?, ?, ?, ?)
        ''', (rule_name, json.dumps(keywords), weightage, rule_type))
        
        conn.commit()
        conn.close()
        
        flash('Scoring rule added successfully!')
        return redirect(url_for('scoring_rules'))
    
    # Get existing rules
    conn = sqlite3.connect('hr_analyzer.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM scoring_rules ORDER BY created_at DESC')
    rules = cursor.fetchall()
    conn.close()
    
    return render_template('scoring_rules.html', rules=rules)

@app.route('/candidates')
def candidates():
    conn = sqlite3.connect('hr_analyzer.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM candidates ORDER BY total_score DESC')
    candidates = cursor.fetchall()
    conn.close()
    
    return render_template('candidates.html', candidates=candidates)

@app.route('/job-posting', methods=['GET', 'POST'])
def job_posting():
    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        candidate_limit = int(request.form['candidate_limit'])
        email_template = request.form['email_template']
        
        conn = sqlite3.connect('hr_analyzer.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO job_postings (title, description, candidate_limit, email_template)
            VALUES (?, ?, ?, ?)
        ''', (title, description, candidate_limit, email_template))
        
        conn.commit()
        conn.close()
        
        flash('Job posting created successfully!')
        return redirect(url_for('job_posting'))
    
    return render_template('job_posting.html')

@app.route('/select-candidates/<int:job_id>')
def select_candidates(job_id):
    conn = sqlite3.connect('hr_analyzer.db')
    cursor = conn.cursor()
    
    # Get job details
    cursor.execute('SELECT * FROM job_postings WHERE id = ?', (job_id,))
    job = cursor.fetchone()
    
    if not job:
        flash('Job posting not found')
        return redirect(url_for('candidates'))
    
    # Get top candidates
    cursor.execute('''
        SELECT * FROM candidates 
        WHERE status = 'pending' 
        ORDER BY total_score DESC 
        LIMIT ?
    ''', (job[3],))  # job[3] is candidate_limit
    
    selected_candidates = cursor.fetchall()
    conn.close()
    
    return render_template('select_candidates.html', job=job, candidates=selected_candidates)

@app.route('/send-invitations/<int:job_id>')
def send_invitations(job_id):
    conn = sqlite3.connect('hr_analyzer.db')
    cursor = conn.cursor()
    
    # Get job details
    cursor.execute('SELECT * FROM job_postings WHERE id = ?', (job_id,))
    job = cursor.fetchone()
    
    # Get top candidates
    cursor.execute('''
        SELECT * FROM candidates 
        WHERE status = 'pending' 
        ORDER BY total_score DESC 
        LIMIT ?
    ''', (job[3],))
    
    selected_candidates = cursor.fetchall()
    
    success_count = 0
    for candidate in selected_candidates:
        if email_service.send_interview_invitation(candidate[2], candidate[1], job[4]):
            # Update candidate status
            cursor.execute('UPDATE candidates SET status = ? WHERE id = ?', 
                         ('invited', candidate[0]))
            success_count += 1
    
    conn.commit()
    conn.close()
    
    flash(f'Invitations sent to {success_count} candidates!')
    return redirect(url_for('candidates'))

@app.route('/api/candidate-details/<int:candidate_id>')
def candidate_details(candidate_id):
    conn = sqlite3.connect('hr_analyzer.db')
    cursor = conn.cursor()
    
    # Get candidate details
    cursor.execute('SELECT * FROM candidates WHERE id = ?', (candidate_id,))
    candidate = cursor.fetchone()
    
    # Get candidate scores
    cursor.execute('''
        SELECT sr.rule_name, cs.score, cs.matched_keywords
        FROM candidate_scores cs
        JOIN scoring_rules sr ON cs.rule_id = sr.id
        WHERE cs.candidate_id = ?
    ''', (candidate_id,))
    scores = cursor.fetchall()
    
    conn.close()
    
    if candidate:
        return jsonify({
            'candidate': candidate,
            'scores': scores
        })
    else:
        return jsonify({'error': 'Candidate not found'}), 404

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
