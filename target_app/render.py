"""HTML for MERIDIAN CORE.

The markup is deliberately bad, in the specific ways 2000s-era bank back-office
software is bad, because that is the surface this project claims to handle:

  * a ``<frameset>`` shell, so "the page" is really three documents
  * table-driven layout with ``<font>`` and nested tables
  * no ``data-testid``, no ARIA, no ``<label for=...>`` -- a field's name lives
    in the ``<td>`` to its left
  * element ids regenerated on every render (``ctl_9f21a``), so any locator
    built from an id is dead on the next request
  * cryptic control names (``f_mid``, ``btnSrch``) and ``<a onclick=...>``
    pseudo-buttons

Everything an automation needs to find here has to be found the way a person
finds it: by what it is and what it is called on screen.
"""

from __future__ import annotations

import random
import string
from html import escape

from .data import Account, Member
from .tenants import Tenant


def rid() -> str:
    """A fresh, useless element id -- regenerated on every single render."""
    return "ctl_" + "".join(random.choices(string.hexdigits.lower(), k=5))


def _chrome(tenant: Tenant, title: str, body: str, *, banner: str = "") -> str:
    return f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><title>{escape(tenant.product)} - {escape(title)}</title>
<style type="text/css">
 body {{ font-family: Verdana, Arial, sans-serif; font-size: 11px; background:#e8e8e8; margin:0; }}
 table.grid {{ border-collapse: collapse; }}
 table.grid td, table.grid th {{ border: 1px solid #b0b0b0; padding: 3px 6px; font-size: 11px; }}
 th {{ background: {tenant.accent}; color: #fff; text-align: left; }}
 .hdr {{ background: {tenant.accent}; color:#fff; padding:6px 10px; font-weight:bold; }}
 .err {{ color:#8b0000; font-weight:bold; }}
 .fld {{ border:1px solid #666; font-size:11px; }}
</style></head>
<body>
<div class="hdr">{escape(tenant.institution)} &nbsp;|&nbsp; {escape(tenant.product)}
 {escape(tenant.product_version)}</div>
{banner}
<table width="100%" cellpadding="8" cellspacing="0"><tr><td valign="top">
{body}
</td></tr></table>
</body></html>"""


def login_page(tenant: Tenant, error: str = "") -> str:
    u, p = rid(), rid()
    err = f'<tr><td colspan="2"><span class="err">{escape(error)}</span></td></tr>' if error else ""
    body = f"""
<table cellpadding="4" cellspacing="0"><tr><td>
 <b>Operator Sign On</b>
 <form method="POST" action="/signon">
 <table cellpadding="3" cellspacing="0">
  {err}
  <tr><td><font size="1">Operator ID</font></td>
      <td><input type="text" name="f_uid" id="{u}" class="fld" size="22"></td></tr>
  <tr><td><font size="1">Passcode</font></td>
      <td><input type="password" name="f_pwd" id="{p}" class="fld" size="22"></td></tr>
  <tr><td></td><td><input type="submit" name="btnLogon" value="Sign On"></td></tr>
 </table></form>
</td></tr></table>"""
    return _chrome(tenant, "Sign On", body)


def console_frameset(tenant: Tenant) -> str:
    """The shell. Note: the real content lives two documents deep."""
    return f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Frameset//EN">
<html><head><title>{escape(tenant.product)} - Member Services</title></head>
<frameset rows="54,*" border="0" frameborder="0">
  <frame src="/frame/banner" name="banner" scrolling="no">
  <frameset cols="180,*" border="1">
    <frame src="/frame/nav" name="nav">
    <frame src="/members/search" name="content">
  </frameset>
</frameset>
</html>"""


def banner_frame(tenant: Tenant, operator: str) -> str:
    return f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><body style="margin:0;font-family:Verdana;font-size:11px">
<div style="background:{tenant.accent};color:#fff;padding:8px 10px;font-weight:bold">
 {escape(tenant.institution)} &nbsp;|&nbsp; Member Services
 <span style="float:right;font-weight:normal">Operator: {escape(operator)}
  &nbsp; <a href="/signoff" target="_top" style="color:#fff">Sign Off</a></span>
</div></body></html>"""


def nav_frame(tenant: Tenant) -> str:
    items = [("Member Search", "/members/search"), ("Batch Queue", "/stub/batch"),
             ("Reports", "/stub/reports"), ("Teller Ops", "/stub/teller")]
    links = "".join(
        f'<tr><td><a href="{href}" target="content" '
        f'style="font-size:11px;text-decoration:none">{escape(label)}</a></td></tr>'
        for label, href in items)
    return f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><body style="margin:0;font-family:Verdana;background:#f4f4f4">
<table cellpadding="6" cellspacing="0" width="100%">{links}</table></body></html>"""


def search_page(tenant: Tenant, error: str = "", prior: str = "") -> str:
    f = rid()
    err = (f'<tr><td colspan="2"><span class="err">{escape(error)}</span></td></tr>'
           if error else "")
    # The field's name is a <td> to its left. There is no <label>, no aria-label,
    # and the id changes on every render.
    body = f"""
<b>Member Inquiry</b><br><br>
<form method="GET" action="/members/detail">
<table cellpadding="4" cellspacing="0">
 {err}
 <tr>
   <td nowrap><font size="1">{tenant.member_id_label}</font></td>
   <td><input type="text" name="f_mid" id="{f}" class="fld" size="14"
        value="{escape(prior)}"></td>
   <td><input type="submit" name="btnSrch" value="{escape(tenant.search_button)}"></td>
 </tr>
</table>
</form>
<br><font size="1" color="#666">Enter the {escape(tenant.member_id_label.lower())}
 to retrieve the member relationship summary.</font>"""
    return _chrome(tenant, "Member Inquiry", body)


def _account_row(tenant: Tenant, a: Account) -> str:
    cells = {
        "Type": escape(a.kind),
        "Account Number": escape(a.number),
        "Balance": f"{a.balance:,.2f}",
        "Status": escape(a.status),
    }
    return "<tr>" + "".join(f"<td>{cells[c]}</td>" for c in tenant.account_columns) + "</tr>"


def member_detail_page(tenant: Tenant, m: Member, notice: str = "") -> str:
    heads = "".join(f"<th>{c}</th>" for c in tenant.account_columns)
    rows = "".join(_account_row(tenant, a) for a in m.accounts)
    notice_html = f'<p class="err">{escape(notice)}</p>' if notice else ""
    body = f"""
<b>Member Relationship Summary</b>{notice_html}
<table cellpadding="0" cellspacing="0"><tr><td>
 <table class="grid" cellpadding="3" cellspacing="0">
  <tr><td nowrap><font size="1">Member Name</font></td><td nowrap><b>{escape(m.name)}</b></td>
      <td nowrap><font size="1">{tenant.member_id_label}</font></td><td nowrap><b>{escape(m.member_id)}</b></td></tr>
  <tr><td nowrap><font size="1">Status</font></td><td nowrap>{escape(m.status)}</td>
      <td nowrap><font size="1">Branch</font></td><td nowrap>{escape(m.branch)}</td></tr>
 </table>
</td></tr></table>
<br><b>{tenant.accounts_heading}</b><br>
<table class="grid" cellpadding="3" cellspacing="0" width="560">
 <tr>{heads}</tr>
 {rows}
</table>
<br>
<a href="/members/subaccount/new?f_mid={escape(m.member_id)}"
   onclick="return true;" style="font-size:11px">Open Sub-Account</a>
&nbsp;|&nbsp;
<a href="/members/search" style="font-size:11px">New Inquiry</a>"""
    return _chrome(tenant, "Member Summary", body)


def subaccount_form_page(tenant: Tenant, m: Member, error: str = "",
                         kind: str = "", nickname: str = "", deposit: str = "") -> str:
    from .data import SUB_ACCOUNT_TYPES
    opts = "".join(
        f'<option value="{escape(t)}"{" selected" if t == kind else ""}>{escape(t)}</option>'
        for t in SUB_ACCOUNT_TYPES)
    err = (f'<tr><td colspan="2"><span class="err">{escape(error)}</span></td></tr>'
           if error else "")
    body = f"""
<b>Open Sub-Account</b> &nbsp; <font size="1">Member {escape(m.member_id)} -
 {escape(m.name)}</font><br><br>
<form method="POST" action="/members/subaccount/review">
<input type="hidden" name="f_mid" value="{escape(m.member_id)}">
<table cellpadding="4" cellspacing="0">
 {err}
 <tr><td nowrap><font size="1">Account Type</font></td>
     <td><select name="f_type" id="{rid()}" class="fld">{opts}</select></td></tr>
 <tr><td nowrap><font size="1">Nickname</font></td>
     <td><input type="text" name="f_nick" id="{rid()}" class="fld" size="24"
          value="{escape(nickname)}"></td></tr>
 <tr><td nowrap><font size="1">Initial Deposit</font></td>
     <td><input type="text" name="f_amt" id="{rid()}" class="fld" size="12"
          value="{escape(deposit)}"></td></tr>
 <tr><td></td><td><input type="submit" name="btnGo" value="Continue"></td></tr>
</table></form>"""
    return _chrome(tenant, "Open Sub-Account", body)


def subaccount_review_page(tenant: Tenant, m: Member, kind: str, nickname: str,
                           deposit: float) -> str:
    body = f"""
<b>Review and Confirm</b><br><br>
<font size="1">This will open a new account on the core. This action cannot be
 reversed from this screen.</font><br><br>
<table class="grid" cellpadding="3" cellspacing="0">
 <tr><td><font size="1">Member</font></td><td>{escape(m.name)} ({escape(m.member_id)})</td></tr>
 <tr><td><font size="1">Account Type</font></td><td>{escape(kind)}</td></tr>
 <tr><td><font size="1">Nickname</font></td><td>{escape(nickname)}</td></tr>
 <tr><td><font size="1">Initial Deposit</font></td><td>{deposit:,.2f}</td></tr>
</table><br>
<form method="POST" action="/members/subaccount/commit">
 <input type="hidden" name="f_mid" value="{escape(m.member_id)}">
 <input type="hidden" name="f_type" value="{escape(kind)}">
 <input type="hidden" name="f_nick" value="{escape(nickname)}">
 <input type="hidden" name="f_amt" value="{deposit:.2f}">
 <input type="submit" name="btnCommit" value="Confirm &amp; Open Account">
 &nbsp;<input type="button" name="btnBack" value="Cancel"
   onclick="window.location='/members/detail?f_mid={escape(m.member_id)}'">
</form>"""
    return _chrome(tenant, "Review", body)


def subaccount_done_page(tenant: Tenant, m: Member, a: Account) -> str:
    body = f"""
<b>Sub-Account Opened</b><br><br>
<table class="grid" cellpadding="3" cellspacing="0">
 <tr><td><font size="1">New Account Number</font></td><td><b>{escape(a.number)}</b></td></tr>
 <tr><td><font size="1">Account Type</font></td><td>{escape(a.kind)}</td></tr>
 <tr><td><font size="1">Opening Balance</font></td><td>{a.balance:,.2f}</td></tr>
 <tr><td><font size="1">Member</font></td><td>{escape(m.name)} ({escape(m.member_id)})</td></tr>
</table><br>
<a href="/members/detail?f_mid={escape(m.member_id)}" style="font-size:11px">
 Return to Member Summary</a>"""
    return _chrome(tenant, "Confirmation", body)


def interstitial_page(tenant: Tenant, message: str, continue_to: str) -> str:
    """The unexpected modal. Blocks the flow until somebody clicks through."""
    body = f"""
<table class="grid" cellpadding="10" cellspacing="0" width="470"><tr><td>
 <b>System Notice</b><br><br>
 <font size="1">{escape(message)}</font><br><br>
 <form method="GET" action="{escape(continue_to)}">
  <input type="submit" name="btnAck" value="Acknowledge">
 </form>
</td></tr></table>"""
    return _chrome(tenant, "System Notice", body)


def error_page(tenant: Tenant, code: str, message: str) -> str:
    body = f"""
<table class="grid" cellpadding="10" cellspacing="0" width="470"><tr><td>
 <span class="err">Application Error {escape(code)}</span><br><br>
 <font size="1">{escape(message)}</font>
</td></tr></table>"""
    return _chrome(tenant, "Error", body)


def stub_page(tenant: Tenant, name: str) -> str:
    return _chrome(tenant, name, f"<b>{escape(name)}</b><br><br>"
                                f'<font size="1">Not implemented in this fixture.</font>')
