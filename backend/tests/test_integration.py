import requests
import sys

def run_integration_test():
    print("Starting integration test against http://127.0.0.1:8000 ...")
    
    # 1. Google Login (Sandbox)
    login_url = "http://127.0.0.1:8000/auth/google"
    payload = {"id_token": "sandbox_integ_test@example.com:Integration Tester:https://avatar.url"}
    
    r = requests.post(login_url, json=payload, timeout=5)
    if r.status_code != 200:
        print(f"FAILED: /auth/google returned status {r.status_code}: {r.text}")
        sys.exit(1)
        
    print("SUCCESS: Google login completed successfully!")
    data = r.json()
    access_token = data["access_token"]
    refresh_cookie = r.cookies.get("refresh_token")
    
    assert access_token is not None
    assert refresh_cookie is not None
    print(f"Tokens received. Access token len: {len(access_token)}, Refresh cookie: {refresh_cookie[:10]}...")
    
    # 2. Get Profile (/auth/me)
    headers = {"Authorization": f"Bearer {access_token}"}
    me_url = "http://127.0.0.1:8000/auth/me"
    
    r_me = requests.get(me_url, headers=headers, timeout=5)
    if r_me.status_code != 200:
        print(f"FAILED: /auth/me returned status {r_me.status_code}: {r_me.text}")
        sys.exit(1)
        
    me_data = r_me.json()
    print(f"SUCCESS: Profile fetched for: {me_data['email']} ({me_data['name']}) | Role: {me_data['role']}")
    assert me_data["email"] == "integ_test@example.com"
    
    # 3. Refresh Session (/auth/refresh)
    refresh_url = "http://127.0.0.1:8000/auth/refresh"
    r_ref = requests.post(refresh_url, headers={"X-Refresh-Token": refresh_cookie}, timeout=5)
    if r_ref.status_code != 200:
        print(f"FAILED: /auth/refresh returned status {r_ref.status_code}: {r_ref.text}")
        sys.exit(1)
        
    ref_data = r_ref.json()
    new_access = ref_data["access_token"]
    new_refresh = r_ref.cookies.get("refresh_token")
    
    assert new_access is not None
    assert new_refresh is not None
    assert new_refresh != refresh_cookie
    print("SUCCESS: Refresh Token Rotation (RTR) verification passed!")
    
    # 4. Active Sessions List (/auth/sessions)
    session_url = "http://127.0.0.1:8000/auth/sessions"
    r_sessions = requests.get(session_url, headers={"Authorization": f"Bearer {new_access}"}, timeout=5)
    assert r_sessions.status_code == 200
    sessions = r_sessions.json()
    print(f"SUCCESS: Active sessions: {len(sessions)}")
    for s in sessions:
        print(f"  - Device: {s['device_name']}, OS: {s['os']}, Browser: {s['browser']}")
        
    # 5. Logout
    logout_url = "http://127.0.0.1:8000/auth/logout"
    r_out = requests.post(logout_url, headers={"Authorization": f"Bearer {new_access}"}, timeout=5)
    assert r_out.status_code == 200
    print("SUCCESS: Logout current session verification passed!")
    
    # Verify token is now invalid
    r_me_expired = requests.get(me_url, headers={"Authorization": f"Bearer {new_access}"}, timeout=5)
    assert r_me_expired.status_code == 401
    print("SUCCESS: Token revocation verification passed!")
    print("INTEGRATION TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    run_integration_test()
