C01 fixture: extracted CodeAgent read_file tool.

Only edit codeagent/tools/read.py. offset is 1-based and nonpositive offsets clamp to the first line. limit bounds returned lines. Output uses actual 1-based line numbers. Preserve existing truncation notices, empty-file and error behavior.

Example: for alpha/beta/gamma/delta lines, offset=2, limit=2 starts with 2\tbeta then 3\tgamma. A request beyond the last line returns (empty file).
