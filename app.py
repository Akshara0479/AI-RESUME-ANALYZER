import os
import json
import requests
from flask import Flask, render_template, request, redirect, url_for, make_response, send_file, Response
from werkzeug.utils import secure_filename

from config_module import SECRET_KEY, UPLOAD_FOLDER, SERVER_IP, SERVER_PORT, extract_text_from_pdf, analyze_resume, ats_prompt, resume_builder_prompt, OLLAMA_URL, MODEL
from database import (
    ensure_db, create_user, get_user_by_credentials, 
    save_resume_to_history, get_user_history, delete_user_history_entry,
    get_all_users, get_all_resumes, delete_user,
    check_token_limit, increment_token, get_user_by_id,
    update_user_token_limit, reset_user_tokens, update_user_status,
    save_generated_pdf, get_generated_pdfs, log_error, get_error_logs,
    get_analytics_summary, get_daily_stats
)
from config_module.prompts import ats_prompt, job_match_prompt, cover_letter_prompt, resume_builder_prompt
from auth import generate_jwt, get_current_user
from resume_export import export_resume
from latex_renderer import render_latex

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ensure_db()

@app.route("/register", methods=["POST"])
def register():
    username = request.form.get("username")
    password = request.form.get("password")

    if not username or not password:
        return render_template("index.html", error="All fields required")

    if not create_user(username, password):
        return render_template("index.html", error="User already exists")

    return redirect(url_for("login"))

@app.route("/")
def home():
    return render_template("homepage.html")

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            return render_template("index.html", error="Username and password required")

        user = get_user_by_credentials(username, password)

        if user:
            if "error" in user:
                return render_template("index.html", error=user["error"])
                
            token = generate_jwt(username, role=user.get("role", "user"))
            resp = make_response(redirect(url_for("dashboard")))
            resp.set_cookie("token", token, httponly=True, samesite="Lax")
            return resp

        return render_template("index.html", error="Invalid credentials")

    return render_template("index.html")

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    payload = get_current_user()

    if not payload:
        return redirect(url_for("login"))

    # Fetch updated token counts for the user
    can_use, tokens_left = check_token_limit(payload["sub"])
    payload["tokens_left"] = tokens_left
    
    # We also need the token_limit for display
    user_data = get_user_by_id(next((u['id'] for u in get_all_users() if u['username'] == payload["sub"]), None))
    if user_data:
        payload["tokens_used"] = user_data["tokens_used"]
        payload["token_limit"] = user_data["token_limit"]

    if request.method == "POST":
        if not can_use:
            return render_template("dashboard.html", error="Token limit reached. Please contact admin to raise your limit.", payload=payload)

        if "resume" not in request.files:
            return render_template("dashboard.html", error="No file uploaded", payload=payload)
            
        file = request.files["resume"]
        if file.filename == "":
            return render_template("dashboard.html", error="No file selected", payload=payload)
            
        import uuid
        uid = uuid.uuid4().hex
        filename = secure_filename(file.filename)
        unique_filename = f"{uid}_{filename}"
        path = os.path.join(UPLOAD_FOLDER, unique_filename)
        file.save(path)

        resume_text = extract_text_from_pdf(path)
        result = analyze_resume(ats_prompt(resume_text))

        save_resume_to_history(payload["sub"], filename, result)
        increment_token(payload["sub"])

        return render_template("dashboard.html", result=result, payload=payload)

    return render_template("dashboard.html", payload=payload)

@app.route("/history")
def history():
    payload = get_current_user()

    if not payload:
        return redirect(url_for("login"))

    rows = get_user_history(payload["sub"])

    return render_template("history.html", payload=payload, history=rows)

@app.route("/history/delete/<int:rid>", methods=["POST"])
def delete_history(rid):
    payload = get_current_user()

    if not payload:
        return redirect(url_for("login"))

    delete_user_history_entry(rid, payload["sub"])

    return redirect(url_for("history"))

@app.route("/extract-text", methods=["POST"])
def extract_text():
    if "resume" not in request.files:
        return "No file", 400
    file = request.files["resume"]
    import uuid
    path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4().hex}_{secure_filename(file.filename)}")
    file.save(path)
    text = extract_text_from_pdf(path)
    return {"text": text}

@app.route("/analyze-stream", methods=["POST"])
def analyze_stream():
    payload = get_current_user()
    if not payload:
        return "", 401

    can_use, tokens_left = check_token_limit(payload["sub"])
    if not can_use:
        return json.dumps({"error": "Token limit reached. Please contact admin to raise your limit."}), 403

    if "resume" not in request.files:
        return "No file uploaded", 400
        
    file = request.files["resume"]
    if file.filename == "":
        return "No file selected", 400
        
    import uuid
    uid = uuid.uuid4().hex
    filename = secure_filename(file.filename)
    unique_filename = f"{uid}_{filename}"
    path = os.path.join(UPLOAD_FOLDER, unique_filename)
    file.save(path)

    resume_text = extract_text_from_pdf(path)
    prompt = ats_prompt(resume_text)

    def stream():
        data = {
            "model": MODEL,
            "prompt": prompt,
            "stream": True
        }

        r = requests.post(OLLAMA_URL, json=data, stream=True)
        final_output = []

        for line in r.iter_lines():
            if line:
                chunk = json.loads(line.decode())
                if "response" in chunk:
                    text = chunk["response"]
                    final_output.append(text)
                    yield text

        import re
        full_text = "".join(final_output)
        match = re.search(r"ATS Score: (\d+)", full_text)
        score = int(match.group(1)) if match else 0
        
        save_resume_to_history(payload["sub"], filename, full_text, score=score)
        increment_token(payload["sub"])

    return Response(stream(), mimetype="text/event-stream")

@app.route("/builder", methods=["GET", "POST"])
def builder():

    payload = get_current_user()
    if not payload:
        return redirect(url_for("login"))

    if request.method == "POST":
        template_name = request.form.get("template","classic")
        payload = get_current_user()

        data = {
            "name": request.form.get("name"),
            "email": request.form.get("email"),
            "phone": request.form.get("phone"),
            "linkedin": request.form.get("linkedin"),
            "github": request.form.get("github"),
            "insta": request.form.get("insta"),
            "x_twitter": request.form.get("x_twitter"),
            "website": request.form.get("website"),
            "subtitle_line": request.form.get("subtitle_line"),
            "summary": request.form.get("summary"),
            "education": request.form.get("education"),
            "skills": request.form.get("skills","").split(","),
            "experience": [line.strip() for line in request.form.get("experience", "").split("\n") if line.strip()],
            "projects": [line.strip() for line in request.form.get("projects", "").split("\n") if line.strip()]
        }
        
        # Handle Photo Upload
        if "photo" in request.files:
            photo = request.files["photo"]
            if photo and photo.filename != "":
                filename = secure_filename(photo.filename)
                photo_path = os.path.join(UPLOAD_FOLDER, filename)
                photo.save(photo_path)
                data["photo_path"] = photo_path
        
        # Collect dynamic fields
        dynamic_keys = request.form.getlist("dynamic_keys[]")
        dynamic_values = request.form.getlist("dynamic_values[]")
        
        dynamic_fields_latex = ""
        for k, v in zip(dynamic_keys, dynamic_values):
            if k.strip() and v.strip():
                # Store in data for conditional checks (e.g., <<IF_CERTIFICATIONS>>)
                safe_key = k.strip().upper().replace(" ", "_")
                data[safe_key] = v
                dynamic_fields_latex += f"\\section*{{{k}}}\n{v}\n"
        
        data["dynamic_fields"] = dynamic_fields_latex

        pdf_path = render_latex(data, template_name)

        if not pdf_path:
            # Check for error log
            log_msg = "LaTeX Compilation Failed"
            # It's hard to get the exact uid here without refactoring latex_renderer, so we just log a generic failure
            log_error("Builder PDF Generation", "Failed to compile LaTeX PDF. Possibly invalid characters.", payload["sub"])

            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return {"success": False, "error": "Failed to generate PDF. Please check your inputs for special characters or contact support."}
            return render_template("builder.html", payload=payload, error="Failed to generate PDF. Please check your inputs for special characters or contact support.")

        # Successfully generated
        save_generated_pdf(payload["sub"], pdf_path)

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            filename_with_uid = pdf_path.replace("compiled/", "")
            return {"success": True, "pdf_url": url_for("serve_compiled", filename=filename_with_uid), "pdf_path": pdf_path}

        return redirect(url_for("preview_pdf", pdf_path=pdf_path))

    return render_template("builder.html", payload=payload)

@app.route("/preview-pdf")
def preview_pdf():
    payload = get_current_user()
    if not payload:
        return redirect(url_for("login"))
        
    pdf_path = request.args.get("pdf_path")
    if not pdf_path or not pdf_path.startswith("compiled/"):
        return redirect(url_for("builder"))
        
    return render_template("latex_preview.html", pdf_path=pdf_path)

@app.route("/download", methods=["POST"])
def download_resume():
    payload = get_current_user()
    if not payload:
        return redirect(url_for("login")), 401
        
    resume_text = request.form.get("resume_text")
    format = request.form.get("format", "docx")
    path = export_resume(resume_text, "generated_resume", format=format)
    return send_file(path, as_attachment=True)

# Redundant route removed

@app.route("/generate-portfolio", methods=["POST"])
def generate_portfolio():
    payload = get_current_user()
    if not payload:
        return redirect(url_for("login"))

    data = {
        "name": request.form.get("name"),
        "email": request.form.get("email"),
        "phone": request.form.get("phone"),
        "summary": request.form.get("summary", "Professional Resume"),
        "skills": request.form.get("skills", "").split(","),
        "experience": [line.strip() for line in request.form.get("experience", "").split("\n") if line.strip()],
    }

    rendered = render_template("portfolio_template.html", **data)
    
    import uuid
    path = os.path.join("exports", f"portfolio_{uuid.uuid4().hex}.html")
    os.makedirs("exports", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(rendered)
        
    return send_file(path, as_attachment=True)

@app.route("/compiled/<path:filename>")
def serve_compiled(filename):
    payload = get_current_user()
    if not payload:
        return redirect(url_for("login")), 401
        
    compile_dir = os.path.join(os.getcwd(), "compiled")
    abs_path = os.path.join(compile_dir, filename)
    
    if not os.path.exists(abs_path):
        return "File not found", 404
        
    if request.args.get("download") == "1":
        return send_file(abs_path, as_attachment=True, download_name="resume.pdf")
    
    return send_file(abs_path, mimetype="application/pdf")

@app.route("/job-match", methods=["POST"])
def job_match():
    payload = get_current_user()
    if not payload:
        return redirect(url_for("login"))

    job_description = request.form.get("job_description")
    resume_text = request.form.get("resume_text") # Passed from the frontend hidden field

    if not job_description or not resume_text:
        return "Missing data", 400

    prompt = job_match_prompt(resume_text, job_description)
    
    def stream():
        data = {"model": MODEL, "prompt": prompt, "stream": True}
        r = requests.post(OLLAMA_URL, json=data, stream=True)
        for line in r.iter_lines():
            if line:
                chunk = json.loads(line.decode())
                if "response" in chunk:
                    yield chunk["response"]

    return Response(stream(), mimetype="text/event-stream")

@app.route("/generate-cover-letter", methods=["POST"])
def generate_cover_letter():
    payload = get_current_user()
    if not payload:
        return redirect(url_for("login"))

    job_description = request.form.get("job_description")
    resume_text = request.form.get("resume_text")

    if not job_description or not resume_text:
        return "Missing data", 400

    prompt = cover_letter_prompt(resume_text, job_description)
    
    def stream():
        data = {"model": MODEL, "prompt": prompt, "stream": True}
        r = requests.post(OLLAMA_URL, json=data, stream=True)
        for line in r.iter_lines():
            if line:
                chunk = json.loads(line.decode())
                if "response" in chunk:
                    yield chunk["response"]

    return Response(stream(), mimetype="text/event-stream")

@app.route("/admin")
def admin_dashboard():
    payload = get_current_user()
    if not payload or payload.get("role") != "admin":
        return redirect(url_for("dashboard"))

    summary = get_analytics_summary()
    daily_stats = get_daily_stats()
    
    return render_template("admin.html", payload=payload, summary=summary, daily_stats=daily_stats)

@app.route("/admin/users")
def admin_users():
    payload = get_current_user()
    if not payload or payload.get("role") != "admin":
        return redirect(url_for("dashboard"))

    users = get_all_users()
    return render_template("admin_users.html", payload=payload, users=users)

@app.route("/admin/resumes")
def admin_resumes():
    payload = get_current_user()
    if not payload or payload.get("role") != "admin":
        return redirect(url_for("dashboard"))

    resumes = get_all_resumes()
    generated_pdfs = get_generated_pdfs()
    return render_template("admin_resumes.html", payload=payload, resumes=resumes, generated_pdfs=generated_pdfs)

@app.route("/admin/logs")
def admin_logs():
    payload = get_current_user()
    if not payload or payload.get("role") != "admin":
        return redirect(url_for("dashboard"))

    logs = get_error_logs()
    return render_template("admin_logs.html", payload=payload, logs=logs)

@app.route("/admin/delete-user/<int:uid>", methods=["POST"])
def admin_delete_user(uid):
    payload = get_current_user()
    if not payload or payload.get("role") != "admin":
        return redirect(url_for("dashboard"))

    delete_user(uid)
    return redirect(url_for("admin_users"))

@app.route("/admin/user/<int:uid>", methods=["GET", "POST"])
def admin_user_page(uid):
    payload = get_current_user()
    if not payload or payload.get("role") != "admin":
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        if "suspend" in request.form:
            update_user_status(uid, 0)
        elif "activate" in request.form:
            update_user_status(uid, 1)
        
        new_limit = request.form.get("token_limit", type=int)
        if new_limit is not None:
            update_user_token_limit(uid, new_limit)
        
        if "reset_tokens" in request.form:
            reset_user_tokens(uid)
            
        return redirect(url_for("admin_user_page", uid=uid))

    user = get_user_by_id(uid)
    if not user:
        return redirect(url_for("admin_users"))
        
    history = get_user_history(user['username'])

    return render_template("admin_user.html", payload=payload, user=user, history=history)

@app.route("/logout")
def logout():
    resp = make_response(redirect(url_for("login")))
    resp.delete_cookie("token")
    return resp

if __name__ == "__main__":
    app.run(host=SERVER_IP, port=SERVER_PORT, debug=True)
