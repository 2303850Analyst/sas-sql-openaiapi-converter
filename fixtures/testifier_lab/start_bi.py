"""Start an independent local BI instance; existing Superset stacks stay untouched."""
import os
import subprocess

subprocess.run(['superset','db','upgrade'],check=True)
from superset.app import create_app
from superset.extensions import appbuilder,db
app=create_app()
with app.app_context():
    if not appbuilder.sm.find_user(username='lab'):
        appbuilder.sm.add_user(username='lab',first_name='Synthetic',last_name='Lab',email='lab@example.invalid',role=appbuilder.sm.find_role('Admin'),password='synthetic_lab_only')
        db.session.commit()
subprocess.run(['superset','init'],check=True)
os.execvp('gunicorn',['gunicorn','--bind','0.0.0.0:8088','--workers','1','--threads','4','--timeout','90','superset.app:create_app()'])
