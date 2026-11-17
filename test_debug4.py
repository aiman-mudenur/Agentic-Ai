import os
os.environ['AUTO_SWARM_ON_SUBMIT']='false'
os.environ['AUTO_GRADE_ON_SUBMIT']='false'
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from db.base import SQLModel
from db.session import get_session as prod_get_session
import tempfile
TMP_DB = tempfile.mktemp(suffix='.db')
test_engine = create_engine(f'sqlite:///{TMP_DB}', connect_args={'check_same_thread': False})
SQLModel.metadata.create_all(test_engine)
def override_session():
    with Session(test_engine) as session:
        yield session
from api.main import app
app.dependency_overrides[prod_get_session] = override_session
client = TestClient(app)
from models.auth import User
from api.security import get_password_hash
with Session(test_engine) as s:
    fac = User(username='facp', email='facp@x.edu', full_name='Fac P', role='faculty', hashed_password=get_password_hash('faculty123'))
    s.add(fac)
    s.commit()
    s.refresh(fac)
    fid = fac.id
    stu = User(username='stugh', email='stugh@x.edu', full_name='Stu GH', role='student', hashed_password=get_password_hash('student123'))
    s.add(stu)
    s.commit()
    s.refresh(stu)
    sid = stu.id
print(f'Created faculty: id={fid}, email=facp@x.edu')
print(f'Created student: id={sid}, email=stugh@x.edu')
r = client.post('/auth/login', data={'username': 'facp@x.edu', 'password': 'faculty123'})
print(f'Login: {r.status_code}')
tok = r.json()['access_token']
h = {'Authorization': f'Bearer {tok}'}
r = client.post('/courses/', headers=h, json={'course_code': 'CS100', 'title': 'Test Course'})
print(f'Create course: {r.status_code}, {r.text[:200]}')
aid = r.json()['id']
r = client.post('/assignments/', headers=h, data={'course_code': 'CS100', 'title': 'GitHub Assignment', 'max_marks': 20})
print(f'Create assignment status: {r.status_code}')
try:
    j = r.json()
    print(f'Parsed JSON: {j}')
    if 'id' in j:
        aid2 = j['id']
        print(f'AID from response: {aid2}')
except Exception as e:
    print(f'Failed to parse JSON: {e}')