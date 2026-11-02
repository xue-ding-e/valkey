# Private QA observation extracted unchanged from the reviewed 9.1.3 stream.tcl patch.
# Official source: 29173b67508b2de4c0f0ed551f3b47cf19fc3ca8; no product API change.
# No concurrent, restart, durability, failover, or complete epoch contract is tested.
start_server {tags {"stream"}} {
    test {Selected XINFO metadata and XREAD can alias across stream recreation} {
        # Pinned 9.1.3 observation, not a requirement on future XINFO fields.
        # The same saved cursor is used after two different synthetic histories.
        set key watch-meta-alias
        set cursor 1-0
        set observations {}
        foreach history {continuous recreated} {
            r DEL $key
            assert_equal 1-0 [r XADD $key 1-0 value one]
            assert_equal [list [list $key {{1-0 {value one}}}]] \
                [r XREAD COUNT 1 STREAMS $key 0-0]

            if {$history eq "recreated"} {
                assert_equal 1 [r DEL $key]
                assert_equal 0 [r EXISTS $key]
                assert_equal 2-0 [r XADD $key 2-0 value two]
            }
            assert_equal 3-0 [r XADD $key 3-0 value three]
            if {$history eq "continuous"} {
                # A: 2-0 never existed; trim removes only the already-read 1-0.
                assert_equal {} [r XRANGE $key 2-0 2-0]
            } else {
                # B: old key was deleted; trim removes unread 2-0 in the new key.
                assert_equal {{2-0 {value two}}} [r XRANGE $key 2-0 2-0]
            }
            assert_equal 1 [r XTRIM $key MAXLEN = 1]

            # Compare only these fields, not the entire XINFO reply or storage.
            # These sequential reads do not provide an atomic concurrent snapshot.
            set info [r XINFO STREAM $key]
            set metadata {}
            foreach field {length entries-added max-deleted-entry-id
                           recorded-first-entry-id last-generated-id} {
                lappend metadata [dict get $info $field]
            }
            set reply [r XREAD STREAMS $key $cursor]
            assert_equal {1 2 0-0 3-0 3-0} $metadata
            assert_equal [list [list $key {{3-0 {value three}}}]] $reply
            lappend observations [list $metadata $reply]
        }
        assert_equal [lindex $observations 0] [lindex $observations 1]
        r DEL $key
        # Equality here is not a namespace epoch, gap, or failover guarantee.
    }

}
