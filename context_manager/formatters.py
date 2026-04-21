"""XML formatters for LLM context and response parsing."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional
from .interfaces import Message


@dataclass
class ToolCall:
    """Parsed tool call from LLM XML response."""
    name: str
    parameters: str
    status: str = "pending"


@dataclass
class ParsedResponse:
    """Parsed LLM response in XML format."""
    content: Optional[str] = None
    tool_calls: list[ToolCall] = None
    error: Optional[str] = None


class XMLResponseParser:
    """Parse LLM's XML format output."""

    def parse(self, text: str) -> ParsedResponse:
        """Parse XML text into ParsedResponse."""
        try:
            root = ET.fromstring(text)
            return self._parse_root(root)
        except ET.ParseError:
            return ParsedResponse(content=text)

    def _parse_root(self, root: ET.Element) -> ParsedResponse:
        """Parse XML root element."""
        result = ParsedResponse()

        content_elem = root.find('content')
        if content_elem is not None:
            result.content = content_elem.text

        result.tool_calls = []
        tool_calls_section = root.find('tool_calls')
        if tool_calls_section is not None:
            for tc in tool_calls_section.findall('tool_call'):
                result.tool_calls.append(ToolCall(
                    name=tc.get('name', ''),
                    parameters=tc.findtext('parameters', ''),
                    status=tc.get('status', 'pending')
                ))

        error_elem = root.find('error')
        if error_elem is not None:
            result.error = error_elem.findtext('description')

        return result


class XMLMessageFormatter:
    """Format messages into XML for LLM."""

    def format_for_llm(
        self,
        messages: list[Message],
        token_budget: int = 80000
    ) -> str:
        """Format as unified XML context string."""
        root = ET.Element('llm_context')

        meta = ET.SubElement(root, 'meta')
        ET.SubElement(meta, 'token_budget').text = str(token_budget)
        ET.SubElement(meta, 'format').text = 'xml'

        sys_section = ET.SubElement(root, 'system_section')
        for msg in messages:
            if msg.role.value == 'system':
                sm = ET.SubElement(sys_section, 'system_message')
                sc = ET.SubElement(sm, 'content')
                sc.text = msg.content

        conv = ET.SubElement(root, 'conversation')
        for msg in messages:
            if msg.role.value == 'system':
                continue
            turn = ET.SubElement(conv, 'turn')
            turn.set('role', msg.role.value)
            msg_type = msg.metadata.get('type', 'unknown')
            turn.set('type', msg_type)

            content = ET.SubElement(turn, 'content')
            content.text = msg.content

            if msg.metadata:
                meta_elem = ET.SubElement(turn, 'metadata')
                for k, v in msg.metadata.items():
                    e = ET.SubElement(meta_elem, k)
                    e.text = str(v)

        output_format = ET.SubElement(root, 'output_format')
        ET.SubElement(output_format, 'instruction').text = (
            'Please respond in XML format, root tag <response>, '
            'containing <content> (text response) and <tool_calls> (if tools needed)'
        )

        return ET.tostring(root, encoding='unicode')
