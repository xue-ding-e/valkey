# Local semantic probe, not a proposed change to XREAD or a reliable-watch API.
# Intended for the official Valkey test harness at baseline:
# 7dfa61e04390990da64b9e982c3b0fc0da8c7c82
# All data is synthetic. The harness must start an isolated disposable server.
start_server {tags {"stream" "watch-contract"}} {
    test {Watch contract: retained unread entries are returned normally} {
        r DEL watch-contract
        r XADD watch-contract 1-0 value one
        r XADD watch-contract 2-0 value two
        r XADD watch-contract 3-0 value three
        assert_equal {{watch-contract {{2-0 {value two}} {3-0 {value three}}}}} \
            [r XREAD STREAMS watch-contract 1-0]
    }

    test {Watch contract: exact trimming does not add an XREAD gap marker} {
        r DEL watch-contract
        r XADD watch-contract 1-0 value one
        r XADD watch-contract 2-0 value two
        r XADD watch-contract 3-0 value three
        assert_equal 2 [r XTRIM watch-contract MAXLEN = 1]
        assert_equal {{watch-contract {{3-0 {value three}}}}} \
            [r XREAD STREAMS watch-contract 1-0]
    }

    test {Watch contract: ordinary nonconsecutive IDs are not proof of loss} {
        r DEL watch-contract
        r XADD watch-contract 1-0 value one
        r XADD watch-contract 3-0 value three
        # Same XREAD shape as the trim case, but no entry 2-0 ever existed.
        assert_equal {{watch-contract {{3-0 {value three}}}}} \
            [r XREAD STREAMS watch-contract 1-0]
    }

    test {Watch contract: deleting an unread middle entry is also not signaled} {
        r DEL watch-contract
        r XADD watch-contract 1-0 value one
        r XADD watch-contract 2-0 value two
        r XADD watch-contract 3-0 value three
        assert_equal 1 [r XDEL watch-contract 2-0]
        assert_equal {{watch-contract {{3-0 {value three}}}}} \
            [r XREAD STREAMS watch-contract 1-0]
    }

    test {Watch contract: key recreation does not bind XREAD cursor to an epoch} {
        r DEL watch-contract
        r XADD watch-contract 1-0 value previous-incarnation
        assert_equal 1 [r DEL watch-contract]
        r XADD watch-contract 3-0 value three
        # A namespace epoch must come from a separate agreed contract.
        assert_equal {{watch-contract {{3-0 {value three}}}}} \
            [r XREAD STREAMS watch-contract 1-0]
        r DEL watch-contract
    }
}
