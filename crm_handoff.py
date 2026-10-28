"""Frozen evidence download and optional, explicit native CRM draft creation."""
import base64
import json
import subprocess
from pathlib import Path

from approval_record import validate_agreement, summary
from picpeak_import import CONTAINER, _command


def bridge(payload):
    # The request travels on stdin, not in command-line arguments or environment.
    encoded = base64.b64encode(json.dumps(payload, allow_nan=False).encode()).decode('ascii')
    source = f"const albumRequest = JSON.parse(Buffer.from('{encoded}','base64').toString());\n"
    source += Path(__file__).with_name('crm_bridge.cjs').read_text()
    _command('galleries')  # Validate the configured container name.
    try:
        result = subprocess.run(['docker','exec','-i',CONTAINER,'node','-'],input=source.encode(),
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=50,check=True)
        rows = result.stdout.decode().split('\nALBUM_RESULT:')
        value = json.loads(rows[-1].splitlines()[0])
    except (subprocess.SubprocessError, ValueError, IndexError) as exc:
        raise ValueError('PicPeak CRM could not be reached. No quote was sent. Retry to recover any draft already created.') from exc
    if not value.get('ok'):
        raise ValueError(value.get('error','CRM draft could not be created'))
    return value['value']


def event_id(project):
    source = project.get('source') or {}
    return source.get('event_id')


def status(project):
    return bridge({'operation':'status','event_id':event_id(project)})


def handoff(project):
    if project.get('status') != 'approved' or project.get('sharing_pending'):
        raise ValueError('Confirm an unchanged client approval before preparing the CRM hand-off.')
    record = validate_agreement(project.get('approval_record'))
    return {'schema_version':1,'project_title':project['title'],
            'agreement':record,'summary':summary(record),'crm_quote':project.get('crm_quote')}


def create_draft(project, incoming):
    record = handoff(project)['agreement']
    for key in ('admin_id','customer_id'):
        if type(incoming.get(key)) is not int or incoming[key] < 1:
            raise ValueError('Choose the PicPeak administrator and gallery customer.')
    vat = incoming.get('vat_rate')
    if type(vat) not in (int,float) or not 0 <= vat <= 100:
        raise ValueError('Confirm the VAT rate for this quote.')
    return bridge({'operation':'draft','event_id':event_id(project),'title':project['title'],
                   'agreement':record,'summary':summary(record),
                   **{key:incoming[key] for key in ('admin_id','customer_id','vat_rate')}})
