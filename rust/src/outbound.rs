//! Outbound buffer splitting parity with aeon_mud.terminal_text.
//! The native Telnet sender is not implemented yet; this is the shared policy
//! ready to be used when that sender is introduced.

const OUTBOUND_BUFFER_CHARS: usize = 1024;
const OUTBOUND_SPOOF_PREFIX: &str = "spoof ";

/// Split one outbound input into independently sendable buffers.
///
/// Count Unicode characters, as the current Python reference does. Preserve
/// all whitespace and force a split only when no whitespace fits the buffer.
#[allow(dead_code)]
pub(crate) fn split_outbound_buffers(line: &str) -> Vec<String> {
    let all: Vec<char> = line.chars().collect();
    if all.len() <= OUTBOUND_BUFFER_CHARS {
        return vec![line.to_owned()];
    }

    let assignment = line.split_once('=').and_then(|(head, _)| {
        let prefix_chars = head.chars().count() + 1;
        if !head.is_empty()
            && !head.contains('\r')
            && !head.contains('\n')
            && prefix_chars < OUTBOUND_BUFFER_CHARS
        {
            Some(format!("{head}="))
        } else {
            None
        }
    });

    let (remaining, first_prefix, continuation_prefix) = match assignment {
        Some(prefix) => {
            let text = &line[prefix.len()..];
            (text, prefix.clone(), prefix)
        }
        None => (line, String::new(), OUTBOUND_SPOOF_PREFIX.to_owned()),
    };

    let characters: Vec<char> = remaining.chars().collect();
    let mut result = Vec::new();
    let mut position = 0;
    let mut prefix = first_prefix.as_str();
    while position < characters.len() {
        let capacity = OUTBOUND_BUFFER_CHARS - prefix.chars().count();
        let available = (characters.len() - position).min(capacity);
        let mut count = available;
        if position + available < characters.len() {
            if let Some(index) = characters[position..position + available]
                .iter()
                .rposition(|ch| ch.is_whitespace() || matches!(*ch, '\u{001c}'..='\u{001f}'))
            {
                count = index + 1;
            }
        }
        let payload: String = characters[position..position + count].iter().collect();
        result.push(format!("{prefix}{payload}"));
        position += count;
        prefix = &continuation_prefix;
    }
    result
}

#[cfg(test)]
mod tests {
    use super::{split_outbound_buffers, OUTBOUND_BUFFER_CHARS, OUTBOUND_SPOOF_PREFIX};

    fn reassemble(chunks: &[String], first_prefix: &str, next_prefix: &str) -> String {
        chunks
            .iter()
            .enumerate()
            .map(|(index, chunk)| {
                let prefix = if index == 0 {
                    first_prefix
                } else {
                    next_prefix
                };
                chunk.strip_prefix(prefix).expect("expected chunk prefix")
            })
            .collect()
    }

    fn assert_limit(chunks: &[String]) {
        assert!(chunks
            .iter()
            .all(|chunk| chunk.chars().count() <= OUTBOUND_BUFFER_CHARS));
    }

    #[test]
    fn assignment_repeats_prefix_and_preserves_payload() {
        let prefix = "set description=";
        let payload = "word=one ".repeat(300);
        let chunks = split_outbound_buffers(&format!("{prefix}{payload}"));
        assert!(chunks.len() > 2);
        assert!(chunks.iter().all(|chunk| chunk.starts_with(prefix)));
        assert_limit(&chunks);
        assert_eq!(reassemble(&chunks, prefix, prefix), payload);
        assert!(chunks[..chunks.len() - 1]
            .iter()
            .all(|chunk| chunk.ends_with(' ')));
    }

    #[test]
    fn no_equals_uses_spoof_with_natural_boundaries() {
        let line = format!("{}omega", "alpha beta gamma ".repeat(130));
        let chunks = split_outbound_buffers(&line);
        assert!(chunks.len() > 1);
        assert!(!chunks[0].starts_with(OUTBOUND_SPOOF_PREFIX));
        assert!(chunks[1..]
            .iter()
            .all(|chunk| chunk.starts_with(OUTBOUND_SPOOF_PREFIX)));
        assert_limit(&chunks);
        assert_eq!(reassemble(&chunks, "", OUTBOUND_SPOOF_PREFIX), line);
        assert!(chunks[..chunks.len() - 1]
            .iter()
            .all(|chunk| chunk.ends_with(' ')));
    }

    #[test]
    fn unbroken_oversized_word_hard_splits() {
        let prefix = "description=";
        let payload = "x".repeat(OUTBOUND_BUFFER_CHARS * 2);
        let chunks = split_outbound_buffers(&format!("{prefix}{payload}"));
        assert!(chunks.len() > 2);
        assert_limit(&chunks);
        assert_eq!(reassemble(&chunks, prefix, prefix), payload);
    }

    #[test]
    fn exact_limit_and_empty_are_unchanged() {
        assert_eq!(split_outbound_buffers(""), vec![""]);
        let line = format!("set={}", "x".repeat(OUTBOUND_BUFFER_CHARS - 4));
        assert_eq!(split_outbound_buffers(&line), vec![line]);
    }

    #[test]
    fn whitespace_runs_and_tabs_are_lossless() {
        let prefix = "say=";
        let payload = format!("{}end", "hello  world\t again ".repeat(90));
        let chunks = split_outbound_buffers(&format!("{prefix}{payload}"));
        assert_limit(&chunks);
        assert_eq!(reassemble(&chunks, prefix, prefix), payload);
    }

    #[test]
    fn oversized_assignment_prefix_falls_back_to_spoof() {
        let line = format!("{}=remaining", "x".repeat(OUTBOUND_BUFFER_CHARS));
        let chunks = split_outbound_buffers(&line);
        assert_eq!(chunks[0], "x".repeat(OUTBOUND_BUFFER_CHARS));
        assert!(chunks[1].starts_with(OUTBOUND_SPOOF_PREFIX));
        assert_limit(&chunks);
        assert_eq!(reassemble(&chunks, "", OUTBOUND_SPOOF_PREFIX), line);
    }

    #[test]
    fn unicode_counts_characters_like_python_reference() {
        let prefix = "say=";
        let payload = format!("{}end", "é 中 ".repeat(400));
        let chunks = split_outbound_buffers(&format!("{prefix}{payload}"));
        assert_limit(&chunks);
        assert_eq!(reassemble(&chunks, prefix, prefix), payload);
    }

    #[test]
    fn contract_v1_unbroken_legacy_cases_still_match() {
        let fixture = include_str!("../../contracts/v1/outbound.tsv");
        for row in fixture
            .lines()
            .filter(|row| !row.is_empty() && !row.starts_with('#'))
        {
            let fields: Vec<_> = row.split('\t').collect();
            let character = fields[1];
            let length: usize = fields[2].parse().unwrap();
            let line = character.repeat(length);
            let chunks = split_outbound_buffers(&line);
            let expected: Vec<usize> = fields[3]
                .split(',')
                .map(|part| part.parse().unwrap())
                .collect();
            let actual: Vec<usize> = chunks.iter().map(|part| part.chars().count()).collect();
            assert_eq!(actual, expected, "fixture: {}", fields[0]);
        }
    }

    fn decode_hex(value: &str) -> String {
        if value == "-" {
            return String::new();
        }
        let bytes: Vec<u8> = value
            .as_bytes()
            .chunks_exact(2)
            .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
            .collect();
        String::from_utf8(bytes).unwrap()
    }

    #[test]
    fn shared_splitting_fixtures_match_exact_boundaries_and_payloads() {
        // The same TSV is executed by tests/test_outbound_command_prefix.py.
        let fixture = include_str!("../../contracts/v1/outbound_splitting.tsv");
        let mut tested = 0;
        for line in fixture.lines().filter(|row| !row.is_empty() && !row.starts_with('#')) {
            let fields: Vec<_> = line.split('\t').collect();
            assert_eq!(fields.len(), 7, "{}", line);
            let case = fields[0];
            let prefix = decode_hex(fields[1]);
            let pattern = decode_hex(fields[2]);
            let repeat: usize = fields[3].parse().unwrap();
            let tail = decode_hex(fields[4]);
            let expected: Vec<usize> = fields[5]
                .split(',')
                .map(|value| value.parse().unwrap())
                .collect();
            let mode = fields[6];
            let input = format!("{prefix}{}{tail}", pattern.repeat(repeat));
            let chunks = split_outbound_buffers(&input);
            let actual: Vec<usize> = chunks.iter().map(|chunk| chunk.chars().count()).collect();
            assert_eq!(actual, expected, "boundary mismatch: {case}");
            assert_limit(&chunks);
            if chunks.len() > 1 {
                match mode {
                    "assignment" => {
                        assert!(chunks.iter().all(|chunk| chunk.starts_with(prefix.as_str())), "{case}");
                        assert_eq!(reassemble(&chunks, &prefix, &prefix), format!("{}{tail}", pattern.repeat(repeat)), "{case}");
                    }
                    "spoof" => {
                        assert!(chunks[1..]
                            .iter()
                            .all(|chunk| chunk.starts_with(OUTBOUND_SPOOF_PREFIX)), "{case}");
                        assert_eq!(reassemble(&chunks, "", OUTBOUND_SPOOF_PREFIX), input, "{case}");
                    }
                    _ => panic!("unknown fixture mode: {case}"),
                }
            } else {
                assert_eq!(chunks, vec![input], "{case}");
            }
            tested += 1;
        }
        assert!(tested >= 14);
    }

}
