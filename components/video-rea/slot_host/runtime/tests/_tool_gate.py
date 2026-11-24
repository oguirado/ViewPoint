# SPDX-License-Identifier: AGPL-3.0-only
"""Shared assertions for the offline host mini-tool projection gate."""

import io
import json
from contextlib import redirect_stdout

from slot_host.projections import control_envelope
from slot_host.projections.cli import main as cli
from slot_host.projections.schema import digest, jcs_bytes
from slot_host.runtime import tools


_EXIT_CODES = {'OK': 0, 'SCHEMA_INVALID': 14, 'AVAILABILITY_PARKED': 16}


def assert_tool_gate(test, projector, tool, arguments, *, cli_command, code):
    """Exercise each entry point with the same request and check the full reply."""
    route = f'host.{tool}'
    mcp_name = 'rea_' + tool.replace('.', '_')
    context = projector.context
    envelope = control_envelope(projector.manifest, route, arguments, context=context)
    test.assertEqual((envelope['schema'], envelope['op']), ('vp.control.req/v1', 'rea.slot.project'))
    test.assertEqual(tools.control_request(tool, arguments, context=context), envelope)

    reply = projector.invoke(route, arguments)
    json_reply = projector.handle_envelope(json.loads(jcs_bytes(envelope)))
    mcp_reply = projector.call_tool(mcp_name, arguments)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = cli([*cli_command, '--args', json.dumps(arguments)], projector=projector)

    ok = code == 'OK'
    canonical_reply = jcs_bytes(reply).decode('utf-8')
    test.assertEqual((reply['schema'], reply['op'], reply['request_id'], reply['code']),
                     ('vp.control.reply/v1', 'rea.slot.project', arguments['request_id'], code))
    test.assertIs(reply['ok'], ok)
    if ok:
        test.assertEqual(reply['result']['kind'], 'host_result')
        normalized = projector.prepare(envelope, authenticated_context=context)
        test.assertEqual(reply['effective_request_sha256'], digest(normalized))
    else:
        test.assertEqual(reply['result'], {})
    test.assertEqual(json_reply, reply)
    test.assertIs(mcp_reply['isError'], not ok)
    test.assertEqual(mcp_reply['structuredContent'], reply)
    test.assertEqual(mcp_reply['content'], [{'type': 'text', 'text': canonical_reply}])
    test.assertEqual(exit_code, _EXIT_CODES[code])
    test.assertEqual(stdout.getvalue(), canonical_reply + '\n')
    return reply
