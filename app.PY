from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from werkzeug.utils import secure_filename
from bleach import Cleaner
from bleach.css_sanitizer import CSSSanitizer
from bs4 import BeautifulSoup
import smtplib
from datetime import datetime, timedelta
import random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import os
from datetime import datetime
import pandas as pd
import logging
from sqlalchemy import exc

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////tmp/emails.db'
app.config['UPLOAD_FOLDER'] = '/tmp/uploads'
app.config['SECRET_KEY'] = 'votre_cle_secrete_secure123'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)


# Ajoutez cette classe pour gérer la rotation
class EmailSender:
    def __init__(self):
        self.current_account_index = 0
        self.batch_size = 2  # Nombre d'emails par compte
        self.delay_seconds = 1  # Délai entre les batches

    def get_next_account(self):
        accounts = GmailAccount.query.order_by(GmailAccount.id).all()
        if not accounts:
            return None
            
        # Sélection aléatoire ou rotation
        # Mode aléatoire :
        # return random.choice(accounts)
        
        # Mode rotation :
        account = accounts[self.current_account_index % len(accounts)]
        self.current_account_index += 1
        return account

    def schedule_next_batch(self, email_id):
        scheduler.add_job(
            self.send_batch,
            'date',
            run_date=datetime.now() + timedelta(seconds=self.delay_seconds),
            args=[email_id]
        )

    def send_batch(self, email_id):
        with app.app_context():
            email = ScheduledEmail.query.get(email_id)
            if not email or email.status == 'Completed':
                return

            account = self.get_next_account()
            if not account:
                email.status = 'Failed (No Account)'
                db.session.commit()
                return

            # Récupère les destinataires non envoyés
            recipients = EmailRecipient.query.filter_by(
                scheduled_email_id=email_id,
                status='Pending'
            ).limit(self.batch_size).all()

            for recipient in recipients:
                try:
                    msg = MIMEMultipart()
                    msg['From'] = account.email
                    msg['To'] = recipient.email_address
                    msg['Subject'] = email.subject
                    msg.attach(MIMEText(email.body, 'html'))

                    # Gestion pièces jointes
                    if email.attachments:
                        for filename in email.attachments.split(','):
                            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename.strip())
                            if os.path.exists(file_path):
                                with open(file_path, 'rb') as f:
                                    part = MIMEBase('application', 'octet-stream')
                                    part.set_payload(f.read())
                                    encoders.encode_base64(part)
                                    part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                                    msg.attach(part)

                    # Envoi SMTP
                    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                        server.login(account.email, account.app_password)
                        server.sendmail(account.email, [recipient.email_address], msg.as_string())

                    recipient.status = 'Sent'
                    recipient.sent_at = datetime.utcnow()

                except Exception as e:
                    recipient.status = f'Failed: {str(e)[:100]}'
                    app.logger.error(f"Erreur pour {recipient.email_address}: {str(e)}")

            db.session.commit()

            # Vérifie s'il reste des destinataires
            remaining = EmailRecipient.query.filter_by(
                scheduled_email_id=email_id,
                status='Pending'
            ).count()

            if remaining > 0:
                self.schedule_next_batch(email_id)
            else:
                email.status = 'Completed'
                db.session.commit()

# Initialisez le sender
email_sender = EmailSender()


# Modèles corrigés
class GmailAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    app_password = db.Column(db.String(100), nullable=False)

class ScheduledEmail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='Scheduled')
    attachments = db.Column(db.Text)
    recipients = db.relationship('EmailRecipient', back_populates='scheduled_email', cascade="all, delete-orphan")

class EmailRecipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email_address = db.Column(db.String(200), nullable=False)  # Nom corrigé
    status = db.Column(db.String(20), default='Pending')
    sent_at = db.Column(db.DateTime)
    scheduled_email_id = db.Column(db.Integer, db.ForeignKey('scheduled_email.id'), nullable=False)
    scheduled_email = db.relationship('ScheduledEmail', back_populates='recipients')

scheduler = BackgroundScheduler(daemon=True)
scheduler.start()
def send_email_job(email_id):
    email_sender.send_batch(email_id)


@app.route('/')
def dashboard():
    emails = ScheduledEmail.query.order_by(ScheduledEmail.scheduled_time.desc()).all()
    accounts = GmailAccount.query.all()
    return render_template('dashboard.html', emails=emails, accounts=accounts)

@app.route('/compose', methods=['GET', 'POST'])
def compose():
    if request.method == 'POST':
        try:
            # Vérification fichier destinataires
            if 'recipients_file' not in request.files:
                flash("Aucun fichier de destinataires fourni", "danger")
                return redirect(url_for('compose'))
            
            recipients_file = request.files['recipients_file']
            if recipients_file.filename == '':
                flash("Fichier de destinataires vide", "danger")
                return redirect(url_for('compose'))

            # Lecture fichier Excel
            try:
                df = pd.read_excel(recipients_file)
                if 'email' not in df.columns:
                    flash("Le fichier doit contenir une colonne 'email'", "danger")
                    return redirect(url_for('compose'))
                
                emails = df['email'].dropna().unique().tolist()
                if not emails:
                    flash("Aucune adresse email valide trouvée", "danger")
                    return redirect(url_for('compose'))
                
            except Exception as e:
                flash(f"Erreur de lecture du fichier: {str(e)}", "danger")
                return redirect(url_for('compose'))

            # Gestion des pièces jointes
            filenames = []
            for file in request.files.getlist('attachments'):
                if file.filename != '':
                    try:
                        # Générer un nom de fichier sécurisé
                        filename = f"{datetime.now().timestamp()}_{secure_filename(file.filename)}"
                        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                        
                        # Vérifier la taille du fichier (5MB max)
                        if file.content_length > 5 * 1024 * 1024:
                            flash(f"Fichier {file.filename} trop volumineux (max 5MB)", "warning")
                            continue
                            
                        # Vérifier l'extension
                        allowed_extensions = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'jpg', 'jpeg', 'png', 'gif'}
                        if '.' not in file.filename or file.filename.split('.')[-1].lower() not in allowed_extensions:
                            flash(f"Format non supporté pour {file.filename}", "warning")
                            continue
                            
                        file.save(file_path)
                        filenames.append(filename)
                        
                    except Exception as e:
                        flash(f"Erreur avec {file.filename} : {str(e)}", "warning")
                        
            raw_html = request.form['body']
            # Nettoyage HTML
            css_sanitizer = CSSSanitizer(allowed_css_properties=['color', 'background-color', 'font-size', 'text-align'])

            cleaner = Cleaner(
                tags=['p', 'b', 'i', 'u', 'strike', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                    'a', 'div', 'span', 'br', 'hr', 'ul', 'ol', 'li',
                    'table', 'thead', 'tbody', 'tr', 'th', 'td'],
                attributes={
                    'a': ['href', 'title', 'target'],
                    '*': ['style', 'class']
                },
                css_sanitizer=css_sanitizer
            )
            
            cleaned_html = cleaner.clean(raw_html)

            # Création email avec pièces jointes
            new_email = ScheduledEmail(
                subject=request.form['subject'],
                body=cleaned_html,
                scheduled_time=datetime.strptime(request.form['scheduled_time'], '%Y-%m-%dT%H:%M'),
                status='Scheduled',
                attachments=','.join(filenames) if filenames else None  # Stockage des noms de fichiers
            )

            # Ajout destinataires
            for email_address in emails:
                new_email.recipients.append(EmailRecipient(email_address=email_address))

            db.session.add(new_email)
            db.session.commit()

            # Planification
            scheduler.add_job(
                send_email_job,
                DateTrigger(run_date=new_email.scheduled_time),
                args=[new_email.id],
                id=f'email_{new_email.id}'
            )

            flash('Email planifié avec succès!', 'success')
            return redirect(url_for('dashboard'))

        except exc.SQLAlchemyError as e:
            db.session.rollback()
            flash(f"Erreur de base de données: {str(e)}", "danger")
            return redirect(url_for('compose'))
            
        except Exception as e:
            db.session.rollback()
            flash(f"Erreur inattendue: {str(e)}", "danger")
            return redirect(url_for('compose'))

            return render_template('compose.html')

        except exc.SQLAlchemyError as e:
            db.session.rollback()
            flash(f"Erreur de base de données: {str(e)}", "danger")
            return redirect(url_for('compose'))
            
        except Exception as e:
            db.session.rollback()
            flash(f"Erreur inattendue: {str(e)}", "danger")
            return redirect(url_for('compose'))

    return render_template('compose.html')

@app.route('/email/<int:id>')
def view_email(id):
    email = ScheduledEmail.query.get_or_404(id)
    return render_template('email_details.html', 
                         email=email,
                         body=email.body.replace('"', "'"),
                         attachments=email.attachments.split(',') if email.attachments else [])

@app.route('/email/<int:id>/recipients')
def email_recipients(id):
    email = ScheduledEmail.query.get_or_404(id)
    return render_template('email_recipients.html', 
                         email=email,
                         recipients=email.recipients)

@app.route('/test-send/<int:email_id>')
def test_send(email_id):
    email_sender.send_batch(email_id)
    return "Batch envoyé!"

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/add-account', methods=['POST'])
def add_account():
    email = request.form['email']
    app_password = request.form['app_password']
    
    if GmailAccount.query.filter_by(email=email).first():
        flash('Ce compte existe déjà!', 'error')
    else:
        new_account = GmailAccount(email=email, app_password=app_password)
        db.session.add(new_account)
        db.session.commit()
        flash('Compte ajouté avec succès!', 'success')
    
    return redirect(url_for('dashboard', _anchor='accounts-section'))

@app.route('/delete-account/<int:id>')
def delete_account(id):
    account = GmailAccount.query.get_or_404(id)
    db.session.delete(account)
    db.session.commit()
    flash('Compte supprimé avec succès!', 'success')
    return redirect(url_for('dashboard', _anchor='accounts-section'))

@app.route('/edit-account/<int:id>', methods=['GET', 'POST'])
def edit_account(id):
    account = GmailAccount.query.get_or_404(id)
    
    if request.method == 'POST':
        account.email = request.form['email']
        account.app_password = request.form['app_password']
        db.session.commit()
        flash('Compte modifié avec succès!', 'success')
        return redirect(url_for('dashboard', _anchor='accounts-section'))
    
    return render_template('edit_account.html', account=account)

def create_app():
    with app.app_context():
        db.create_all()
        if not os.path.exists(app.config['UPLOAD_FOLDER']):
            os.makedirs(app.config['UPLOAD_FOLDER'])
    return app
