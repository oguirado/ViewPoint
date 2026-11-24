# SPDX-License-Identifier: AGPL-3.0-only
"""Validation and reference relocation for projection schemas."""

import hashlib
import json
import math
import re
from decimal import Decimal
from urllib.parse import unquote, urljoin, urlsplit


class PublicationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _number(value):
    if isinstance(value, int):
        if abs(value) < 2**53:
            return str(value)
        try:
            converted = float(value)
        except OverflowError as error:
            raise ValueError('Number is outside the canonical JSON range.') from error
        if int(converted) != value:
            raise ValueError('Integer loses precision in canonical JSON.')
        value = converted
    if not math.isfinite(value):
        raise ValueError('Nonfinite JSON number.')
    if value == 0:
        return '0'
    decimal = Decimal(repr(value))
    if 1e-6 <= abs(value) < 1e21:
        fixed = format(decimal, 'f')
        return fixed.rstrip('0').rstrip('.') if '.' in fixed else fixed
    coefficient, exponent = format(decimal, 'e').split('e')
    if '.' in coefficient:
        coefficient = coefficient.rstrip('0').rstrip('.')
    return f'{coefficient}e{int(exponent):+d}'


def _canonical(value):
    kind = type(value)
    if value is None or kind is bool:
        return json.dumps(value)
    if kind is str:
        return json.dumps(value, ensure_ascii=False)
    if kind in (int, float):
        return _number(value)
    if kind is list:
        return '[' + ','.join(map(_canonical, value)) + ']'
    if kind is dict and all(type(key) is str for key in value):
        keys = sorted(value, key=lambda key: key.encode('utf-16-be'))
        return '{' + ','.join(_canonical(key) + ':' + _canonical(value[key]) for key in keys) + '}'
    raise ValueError('Expected a JSON value.')


def jcs_bytes(value):
    try:
        return _canonical(value).encode('utf-8')
    except (ValueError, UnicodeError, RecursionError) as error:
        raise PublicationError('SCHEMA_INVALID', 'Invalid canonical JSON value.') from error


def digest(value):
    return hashlib.sha256(jcs_bytes(value)).hexdigest()


def resource(registry, ref, pin=None):
    if type(ref) is not str or not ref:
        raise PublicationError('SCHEMA_INVALID', 'Schema reference must be a nonempty string.')
    if ref not in registry:
        raise PublicationError('MISSING_PROJECTION', f'Unresolved schema resource {ref}.')
    schema = registry[ref]
    if type(schema) is not dict or schema.get('$id') != ref:
        raise PublicationError('SCHEMA_INVALID', f'Schema identity differs for {ref}.')
    if pin is not None and digest(schema) != pin:
        raise PublicationError('SCHEMA_INVALID', f'Schema digest differs for {ref}.')
    return schema


_SCHEMA_MAPS = {'$defs', 'definitions', 'properties', 'patternProperties', 'dependentSchemas'}
_SCHEMA_ARRAYS = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}
_SCHEMA_CHILDREN = {'items', 'contains', 'additionalProperties', 'propertyNames', 'not', 'if', 'then', 'else'}
_VALUE_KEYS = {
    '$schema', '$id', '$comment', 'title', 'description', 'type', 'const', 'enum',
    'default', 'examples', 'deprecated', 'readOnly', 'writeOnly', 'required',
    'dependentRequired', 'minProperties', 'maxProperties', 'minLength', 'maxLength',
    'pattern', 'format', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
    'multipleOf', 'minItems', 'maxItems', 'uniqueItems', 'minContains', 'maxContains',
}


def _pointer(document, fragment):
    if not fragment:
        return document
    fragment = unquote(fragment)
    if not fragment.startswith('/'):
        raise PublicationError('MISSING_PROJECTION', f'Unsupported schema fragment {fragment}.')
    try:
        for part in fragment[1:].split('/'):
            key = part.replace('~1', '/').replace('~0', '~')
            if type(document) is list:
                if not re.fullmatch(r'0|[1-9][0-9]*', key):
                    raise ValueError('Invalid array pointer.')
                document = document[int(key)]
            else:
                document = document[key]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise PublicationError('MISSING_PROJECTION', f'Unresolved schema pointer {fragment}.') from error
    return document


def _other_control_operation(branch, operation):
    if type(branch) is not dict or set(branch) != {'if', 'then'}:
        return False
    condition = branch['if']
    if type(condition) is not dict:
        return False
    properties = condition.get('properties')
    selector = properties.get('op') if type(properties) is dict else None
    if type(selector) is not dict:
        return False
    if type(selector.get('const')) is str and selector['const'] != operation:
        return True
    choices = selector.get('enum')
    if type(choices) is list and operation not in choices:
        return True
    negated = selector.get('not')
    return (type(negated) is dict and set(negated) == {'enum'}
            and type(negated['enum']) is list and operation in negated['enum'])


def standalone(schema, registry, *, control_op=None):
    """Relocate reachable references into a single wrapper-owned resource."""
    root = schema.get('$id')
    resources = dict(registry)
    resources[root] = schema
    links = {}
    definitions = {}

    def link(ref, base):
        if type(ref) is not str or not ref:
            raise PublicationError('SCHEMA_INVALID', 'Invalid schema reference.')
        address, _, fragment = ref.partition('#')
        resolved_base = urljoin(base, address) if address else base
        key = (resolved_base, fragment)
        if key not in links:
            target = resource(resources, resolved_base)
            name = f'projection_ref_{len(links)}'
            links[key] = name
            definitions[name] = visit(_pointer(target, fragment), resolved_base)
        return '#/$defs/' + links[key]

    def visit(node, base):
        if type(node) is bool:
            return node
        if type(node) is not dict:
            raise PublicationError('SCHEMA_INVALID', 'Expected a schema object or boolean.')
        if '$id' in node and node['$id'] != base:
            raise PublicationError('SCHEMA_INVALID', 'Nested schema resource requires separate registration.')
        result = {}
        for key, value in node.items():
            if key == '$ref':
                result[key] = link(value, base)
            elif key in _SCHEMA_MAPS:
                if type(value) is not dict:
                    raise PublicationError('SCHEMA_INVALID', f'Invalid {key} schema map.')
                if key not in ('$defs', 'definitions'):
                    result[key] = {name: visit(child, base) for name, child in value.items()}
            elif key in _SCHEMA_ARRAYS:
                if type(value) is not list:
                    raise PublicationError('SCHEMA_INVALID', f'Invalid {key} schema array.')
                result[key] = [visit(child, base) for child in value]
            elif key in _SCHEMA_CHILDREN:
                result[key] = visit(value, base)
            elif key in _VALUE_KEYS:
                if key not in ('$id', '$schema'):
                    result[key] = value
            else:
                raise PublicationError('SCHEMA_INVALID', f'Unsupported schema keyword {key}.')
        return result

    if type(root) is not str or not root:
        raise PublicationError('SCHEMA_INVALID', 'A standalone schema requires a resource id.')
    selected = schema
    if control_op is not None:
        branches = schema.get('allOf', [])
        if type(branches) is not list:
            raise PublicationError('SCHEMA_INVALID', 'Invalid allOf schema array.')
        # A selector-pruned schema is valid only for the selected operation.
        discriminator = {'type': 'object', 'required': ['op'],
                         'properties': {'op': {'const': control_op}}}
        selected = dict(schema, allOf=[discriminator, *(
            branch for branch in branches if not _other_control_operation(branch, control_op)
        )])
        resources[root] = selected
    try:
        result = visit(selected, root)
    except (TypeError, ValueError, RecursionError) as error:
        if isinstance(error, PublicationError):
            raise
        raise PublicationError('SCHEMA_INVALID', 'Invalid schema resource.') from error
    result['$id'] = root
    if '$schema' in schema:
        result['$schema'] = schema['$schema']
    if definitions:
        result['$defs'] = definitions
    return result


def accepts(value, schema):
    """Evaluate a compiled schema. Malformed constraints fail the boundary."""
    jcs_bytes(value)
    active_refs = set()

    def matches(item, rule):
        if type(rule) is bool:
            return rule
        if '$ref' in rule:
            ref = rule['$ref']
            if not ref.startswith('#/'):
                raise PublicationError('SCHEMA_INVALID', 'Uncompiled schema reference.')
            key = (ref, id(item))
            if key in active_refs:
                raise PublicationError('SCHEMA_INVALID', 'Schema reference has a nonprogressing cycle.')
            active_refs.add(key)
            try:
                if not matches(item, _pointer(schema, ref[1:])):
                    return False
            finally:
                active_refs.remove(key)
        kind = type(item)
        types = {
            'null': item is None, 'boolean': kind is bool, 'object': kind is dict,
            'array': kind is list, 'string': kind is str, 'number': kind in (int, float),
            'integer': kind is int or (kind is float and item.is_integer()),
        }
        if 'type' in rule:
            declared = rule['type']
            names = [declared] if type(declared) is str else declared
            if not names or any(name not in types for name in names):
                raise PublicationError('SCHEMA_INVALID', 'Invalid schema type.')
            if not any(types[name] for name in names):
                return False
        if 'const' in rule or 'enum' in rule:
            encoded_item = jcs_bytes(item)
            if 'const' in rule and encoded_item != jcs_bytes(rule['const']):
                return False
            if 'enum' in rule and all(encoded_item != jcs_bytes(choice) for choice in rule['enum']):
                return False
        if not all(matches(item, child) for child in rule.get('allOf', [])):
            return False
        if 'anyOf' in rule and not any(matches(item, child) for child in rule['anyOf']):
            return False
        if 'oneOf' in rule and sum(matches(item, child) for child in rule['oneOf']) != 1:
            return False
        if 'not' in rule and matches(item, rule['not']):
            return False
        if 'if' in rule:
            branch = 'then' if matches(item, rule['if']) else 'else'
            if not matches(item, rule.get(branch, True)):
                return False
        if kind is dict:
            if not rule.get('minProperties', 0) <= len(item) <= rule.get('maxProperties', len(item)):
                return False
            if set(rule.get('required', [])) - item.keys():
                return False
            properties = rule.get('properties', {})
            patterns = rule.get('patternProperties', {})
            for key, child in item.items():
                if not matches(key, rule.get('propertyNames', True)):
                    return False
                applicable = [value for pattern, value in patterns.items() if re.search(pattern, key)]
                if key in properties:
                    applicable.append(properties[key])
                if not applicable:
                    applicable.append(rule.get('additionalProperties', True))
                if not all(matches(child, subrule) for subrule in applicable):
                    return False
            for key, dependent in rule.get('dependentRequired', {}).items():
                if key in item and set(dependent) - item.keys():
                    return False
            for key, dependent in rule.get('dependentSchemas', {}).items():
                if key in item and not matches(item, dependent):
                    return False
        if kind is list:
            if not rule.get('minItems', 0) <= len(item) <= rule.get('maxItems', len(item)):
                return False
            if rule.get('uniqueItems') and len(set(map(jcs_bytes, item))) != len(item):
                return False
            prefix = rule.get('prefixItems', [])
            if not all(matches(child, subrule) for child, subrule in zip(item, prefix)):
                return False
            if not all(matches(child, rule.get('items', True)) for child in item[len(prefix):]):
                return False
            if 'contains' in rule:
                count = sum(matches(child, rule['contains']) for child in item)
                if not rule.get('minContains', 1) <= count <= rule.get('maxContains', len(item)):
                    return False
        if kind is str:
            if not rule.get('minLength', 0) <= len(item) <= rule.get('maxLength', len(item)):
                return False
            if 'pattern' in rule and re.search(rule['pattern'], item) is None:
                return False
            if 'format' in rule:
                if rule['format'] not in ('uri', 'uri-reference'):
                    raise PublicationError('SCHEMA_INVALID', f'Unsupported schema format {rule["format"]}.')
                if re.search(r'[^\x21-\x7e]|%(?![0-9a-fA-F]{2})', item):
                    return False
                if rule['format'] == 'uri' and not urlsplit(item).scheme:
                    return False
        if kind in (int, float):
            if 'minimum' in rule and item < rule['minimum']:
                return False
            if 'maximum' in rule and item > rule['maximum']:
                return False
            if 'exclusiveMinimum' in rule and item <= rule['exclusiveMinimum']:
                return False
            if 'exclusiveMaximum' in rule and item >= rule['exclusiveMaximum']:
                return False
            if 'multipleOf' in rule and Decimal(str(item)) % Decimal(str(rule['multipleOf'])):
                return False
        return True

    try:
        return matches(value, schema)
    except PublicationError:
        raise
    except (TypeError, ValueError, KeyError, ArithmeticError, re.error, RecursionError) as error:
        raise PublicationError('SCHEMA_INVALID', 'Malformed or unsupported schema constraint.') from error


def require(value, schema, label):
    if not accepts(value, schema):
        raise PublicationError('SCHEMA_INVALID', f'{label} violates its pinned schema.')
