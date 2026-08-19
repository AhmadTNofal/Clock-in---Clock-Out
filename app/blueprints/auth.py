"""Back-office sign in and sign out."""

from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import BooleanField, PasswordField, StringField
from wtforms.validators import DataRequired, Length

from ..extensions import db
from ..models import AdminUser, utcnow
from ..security import rate_limit

bp = Blueprint("auth", __name__)


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=200)])
    remember = BooleanField("Keep me signed in")


def _safe_next(target: str | None) -> str:
    """Only follow a "next" parameter that points back at this site.

    Without this check, ?next=https://elsewhere.example turns the login page into
    an open redirect.
    """
    if not target:
        return url_for("admin.dashboard")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/"):
        return url_for("admin.dashboard")
    return target


@bp.route("/login", methods=["GET", "POST"])
@rate_limit("login", "LOGIN_RATE_LIMIT", "LOGIN_RATE_WINDOW", as_json=False)
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.scalars(
            select(AdminUser).where(AdminUser.username == form.username.data)
        ).first()

        # The same message for every failure, so the form cannot be used to
        # discover which usernames exist.
        if user is None or not user.check_password(form.password.data or ""):
            flash("Incorrect username or password.", "error")
            return render_template("login.html", form=form), 401
        if not user.is_active:
            flash("That account has been disabled.", "error")
            return render_template("login.html", form=form), 403

        login_user(user, remember=bool(form.remember.data))
        user.last_login_at = utcnow()
        db.session.commit()
        return redirect(_safe_next(request.args.get("next")))

    return render_template("login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("auth.login"))
