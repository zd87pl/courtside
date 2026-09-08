"""Durable per-request accounting, including repairs, retries and uncertain responses."""
import json
import math
import os
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal

from . import db
from .config import settings


class BudgetExceeded(RuntimeError):
    pass


def _provider_get(path):
    s = settings()
    if s.openrouter_url.rstrip('/') != 'https://openrouter.ai/api/v1':
        raise RuntimeError('Provider budget/usage verification requires the OpenRouter endpoint')
    request = urllib.request.Request(s.openrouter_url.rstrip('/') + path,
                                    headers={'Authorization': f'Bearer {s.openrouter_api_key}'})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)['data']


def verify_provider_budget():
    data = _provider_get('/key')
    limit, remaining = data.get('limit'), data.get('limit_remaining')
    if not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
        raise RuntimeError('Set a finite spending limit on the OpenRouter API key before starting the worker')
    if not isinstance(remaining, (int, float)) or not math.isfinite(remaining) or remaining <= 0:
        raise RuntimeError('OpenRouter key has no budget remaining')


def begin(model: str):
    s = settings()
    if model not in (s.allowed_models or (s.default_model,)):
        raise BudgetExceeded('Model is not in ALLOWED_MODELS')
    jid = uuid.UUID(os.environ['COURTSIDE_JOB_ID'])
    attempt = int(os.environ['COURTSIDE_JOB_ATTEMPT'])
    wid = os.environ['COURTSIDE_JOB_WORKER']
    call_id = uuid.uuid4()
    reserve = Decimal(str(s.request_reserve_usd))
    with db.conn() as c, c.transaction():
        job = c.execute('SELECT account_id FROM jobs WHERE id = %s', (jid,)).fetchone()
        if not job:
            raise BudgetExceeded('Job no longer exists')
        # Same lock order as admission: account, then job.
        account = c.execute('SELECT monthly_usd_cap, disabled_at FROM accounts WHERE id = %s FOR UPDATE',
                            (job['account_id'],)).fetchone()
        active = c.execute("""SELECT id FROM jobs WHERE id = %s AND status = 'running'
            AND attempts = %s AND worker_id = %s AND NOT cancel_requested
            AND deletion_requested_at IS NULL FOR UPDATE""", (jid, attempt, wid)).fetchone()
        if not active or account['disabled_at']:
            raise BudgetExceeded('Job lease is no longer authorized')
        used = c.execute("""SELECT COALESCE(sum(COALESCE(actual_usd, reserved_usd)), 0) AS total
            FROM model_usage WHERE account_id = %s AND created_at >= date_trunc('month', now(), 'UTC')""",
            (job['account_id'],)).fetchone()['total']
        if used + reserve > account['monthly_usd_cap']:
            raise BudgetExceeded('Account budget cannot cover the next model request reservation')
        c.execute("""INSERT INTO model_usage(id, account_id, job_id, attempt, model, reserved_usd)
            VALUES (%s, %s, %s, %s, %s, %s)""", (call_id, job['account_id'], jid, attempt, model, reserve))
    return call_id


def finish(call_id, response=None, error=None):
    usage = getattr(response, 'usage', None)
    cost = getattr(usage, 'cost', None)
    if cost is not None:
        try:
            cost = float(cost)
            if not math.isfinite(cost) or cost < 0:
                cost = None
        except (TypeError, ValueError):
            cost = None
    # Explicit request rejection cannot have completed inference. Ambiguous
    # transport/5xx failures keep the reservation until operator reconciliation.
    if error and getattr(error, 'status_code', None) in (400, 401, 403, 404, 422, 429):
        cost = 0
    with db.conn() as c, c.transaction():
        c.execute("""UPDATE model_usage SET actual_usd = %s, state = %s,
            provider_id = %s, prompt_tokens = %s, completion_tokens = %s, finished_at = now()
            WHERE id = %s""", (cost, 'settled' if cost is not None else 'uncertain',
            getattr(response, 'id', None), getattr(usage, 'prompt_tokens', None),
            getattr(usage, 'completion_tokens', None), call_id))
        _refresh_job_cost(c, call_id)


def reconcile():
    with db.conn() as c:
        rows = c.execute("""SELECT id, provider_id FROM model_usage WHERE actual_usd IS NULL
            AND provider_id IS NOT NULL AND created_at < now() - interval '1 minute' LIMIT 25""").fetchall()
    for row in rows:
        try:
            data = _provider_get('/generation?id=' + urllib.parse.quote(row['provider_id'], safe=''))
            cost = float(data['total_cost'])
            if not math.isfinite(cost) or cost < 0:
                continue
            with db.conn() as c:
                c.execute("UPDATE model_usage SET actual_usd = %s, state = 'settled', finished_at = now() WHERE id = %s AND actual_usd IS NULL",
                          (cost, row['id']))
                _refresh_job_cost(c, row['id'])
        except Exception:
            continue  # reservation stays charged; never silently discard an unknown cost


def summary(account_id, job_id=None):
    where = 'account_id = %s'
    params = [account_id]
    if job_id:
        where += ' AND job_id = %s'
        params.append(job_id)
    else:
        where += " AND created_at >= date_trunc('month', now(), 'UTC')"
    with db.conn() as c:
        row = c.execute(f"""SELECT COALESCE(sum(actual_usd), 0) AS confirmed_usd,
            COALESCE(sum(reserved_usd) FILTER (WHERE actual_usd IS NULL), 0) AS reserved_usd,
            count(*) FILTER (WHERE actual_usd IS NULL) AS unsettled_requests,
            count(*) AS requests FROM model_usage WHERE {where}""", tuple(params)).fetchone()
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


def _refresh_job_cost(c, call_id):
    c.execute("""UPDATE jobs j SET cost_usd = (
        SELECT COALESCE(sum(COALESCE(actual_usd, reserved_usd)), 0) FROM model_usage u WHERE u.job_id = j.id)
        WHERE j.id = (SELECT job_id FROM model_usage WHERE id = %s)""", (call_id,))
