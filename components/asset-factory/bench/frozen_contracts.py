"""Consumed frozen F0 shapes; no sibling imports or licence-rights inference.

validate_bundle checks data contracts only. U2 still owns policy evaluation.
"""
import re


def obj(properties, required=None):
    return {'type':'object', 'properties':properties,
            'required':list(properties) if required is None else required,
            'additionalProperties':False}


def array(items, minimum=0):
    return {'type':'array', 'items':items, 'minItems':minimum}


TEXT = {'type':'string'}
BOOL = {'type':'boolean'}
INT = {'type':'integer'}
NULL = {'type':'null'}
HASH = {'type':'string', 'pattern':'^[0-9a-f]{64}$'}
NULL_BOOL = {'type':['boolean','null']}
REF = obj({'path':TEXT, 'sha256':HASH})
SCALAR = {'oneOf':[TEXT, INT, BOOL]}
PARAMS = {'type':'object', 'additionalProperties':SCALAR}
RC = {'enum':[0,2,3,4,5,6]}
VERDICT = {'enum':['PASS','ERROR','FAIL','CANNOT_SEE']}


def schemas(slots):
    dep = obj({'name':TEXT, 'licence':TEXT, 'commercial':NULL_BOOL,
               'territory_excluded':array(TEXT), 'gated':NULL_BOOL,
               'used_by':array({'enum':list(slots)})})
    weights = obj({'repo':TEXT, 'revision':TEXT, 'files':array(REF, 1)})
    provider = obj({'schema':{'const':'af.provider/v1'}, 'id':TEXT,
        'slot':{'enum':list(slots)}, 'kind':{'enum':['model','tool']}, 'runnable':BOOL,
        'revision':{'type':['string','null']},
        'licence':obj({'code':TEXT,'weights':TEXT,'deps':array(dep)}),
        'territory_excluded':array(TEXT), 'commercial':NULL_BOOL, 'gated':NULL_BOOL,
        'weights':array(weights), 'runtime':obj({'container':TEXT,
            'ram_peak_gb_measured':{'type':['integer','null']},
            'ram_peak_measurement_ref':{'oneOf':[NULL, REF]}}),
        'known_issues':array(TEXT), 'blocked_reason':TEXT},
        ['schema','id','slot','kind','runnable','revision','licence','territory_excluded',
         'commercial','gated','weights','runtime','known_issues'])
    assignment = obj({'provider':TEXT, 'params':PARAMS, 'optional':BOOL}, ['provider'])
    factory = obj({'schema':{'const':'af.factory-config/v1'}, 'jurisdiction':TEXT,
        'commercial_use':BOOL, 'models_path':TEXT,
        'memory':obj({'max_resident_models':{'const':1}, 'ceiling_gb_default':INT,
                      'min_free_gb_before_start':INT}),
        'slots':obj({slot:assignment for slot in slots}),
        'gates':obj({'appearance':obj({'floor':INT,'target':INT}), 'vqa3d_profile':TEXT})})
    policy = obj({'schema':{'const':'af.territory-groups/v1'},
        'countries':array(TEXT), 'groups':{'type':'object','additionalProperties':array(TEXT)}})
    decision = obj({'schema':{'const':'af.licence-decision/v1'}, 'rc':RC, 'verdict':VERDICT,
        'allowed':BOOL, 'provider_id':TEXT, 'operation':{'enum':list(slots)},
        'jurisdiction':TEXT, 'commercial_use':BOOL, 'monetisable':BOOL,
        # The frozen interface pins this array's presence, not its item shape.
        'reason_codes':array(TEXT), 'evaluated_dependencies':array({}),
        'configuration_sha256':HASH, 'provider_sha256':HASH, 'policy_sha256':HASH})
    return dict(manifest=provider, factory=factory, policy=policy, decision=decision)


def validate_bundle(bundle, *, schema, require, slots):
    for name, shape in schemas(slots).items():
        schema(bundle[name], shape)
    p, f, policy, d = (bundle[k] for k in ('manifest','factory','policy','decision'))
    require(all(isinstance(v, str) and v.strip() for v in
                (p['id'], f['models_path'], f['gates']['vqa3d_profile'])), 2, 'schema_mismatch')
    countries = policy['countries']
    require(bool(countries) and len(countries) == len(set(countries)) and
            all(re.fullmatch('[A-Z]{2}', v) for v in countries), 2, 'policy_invalid')
    require(f['jurisdiction'] in countries, 2, 'unknown_jurisdiction')
    require(all(group and len(members) == len(set(members)) and
                all(v in countries for v in members)
                for group, members in policy['groups'].items()), 2, 'policy_invalid')
    require(f['memory']['ceiling_gb_default'] > 0 and
            f['memory']['min_free_gb_before_start'] >= 0, 2, 'config_invalid')
    for assignment in f['slots'].values():
        require(bool(assignment['provider'].strip()), 2, 'config_invalid')
    if p['runnable']:
        # The caller's _provider enforces the provider revision pin after
        # rejecting blocked providers, whose revision may legitimately be null.
        require((p['kind'] == 'model' and bool(p['weights'])) or
                (p['kind'] == 'tool' and p['weights'] == []), 2, 'provider_invalid')
    else:
        require(isinstance(p.get('blocked_reason'), str) and bool(p['blocked_reason'].strip()),
                2, 'provider_invalid')
    for weight in p['weights']:
        require(bool(weight['repo'].strip()) and bool(weight['revision'].strip()) and
                weight['revision'] not in ('main','master','latest','HEAD'), 2, 'unpinned_revision')
    peak = p['runtime']['ram_peak_gb_measured']
    reference = p['runtime']['ram_peak_measurement_ref']
    require((peak is None and reference is None) or
            (type(peak) is int and peak >= 0 and reference is not None), 2, 'provider_invalid')
    require(d['allowed'] == (d['rc'] == 0), 2, 'decision_invalid')
