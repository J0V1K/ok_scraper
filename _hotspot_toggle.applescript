-- Toggle Hotspot Shield off then on via UI scripting. Flat single-block
-- to avoid AppleScript handler scoping quirks with UI references.

tell application "Hotspot Shield" to activate
delay 1.2

set didClick to 0
set finalState to ""

tell application "System Events"
    tell process "Hotspot Shield"
        -- Find initial state.
        set startState to ""
        set elems to every UI element of UI element 1 of window 1
        repeat with el in elems
            try
                set d to description of el
                if d is "Disconnect button" or d is "Connect button" then
                    set startState to d
                    exit repeat
                end if
            end try
        end repeat
        if startState is "" then error "toggle button not found"
        log ("start state: " & startState)

        -- Click to disconnect (or connect if already disconnected).
        repeat with el in (every UI element of UI element 1 of window 1)
            try
                set d to description of el
                if d is "Disconnect button" or d is "Connect button" then
                    click el
                    set didClick to didClick + 1
                    exit repeat
                end if
            end try
        end repeat

        -- Wait for state flip.
        if startState is "Disconnect button" then
            set targetMid to "Connect button"
            set targetFinal to "Disconnect button"
        else
            set targetMid to "Disconnect button"
            set targetFinal to "Connect button"
        end if

        set deadline to (current date) + 25
        set cur to ""
        repeat while (current date) < deadline
            repeat with el in (every UI element of UI element 1 of window 1)
                try
                    set d to description of el
                    if d is "Disconnect button" or d is "Connect button" then
                        set cur to d
                        exit repeat
                    end if
                end try
            end repeat
            if cur is targetMid then exit repeat
            delay 0.5
        end repeat
        if cur is not targetMid then error "did not reach " & targetMid

        -- Only do the second click if we actually disconnected first.
        if startState is "Disconnect button" then
            delay 1.5
            repeat with el in (every UI element of UI element 1 of window 1)
                try
                    set d to description of el
                    if d is "Disconnect button" or d is "Connect button" then
                        click el
                        set didClick to didClick + 1
                        exit repeat
                    end if
                end try
            end repeat

            set deadline2 to (current date) + 30
            set cur2 to ""
            repeat while (current date) < deadline2
                repeat with el in (every UI element of UI element 1 of window 1)
                    try
                        set d to description of el
                        if d is "Disconnect button" or d is "Connect button" then
                            set cur2 to d
                            exit repeat
                        end if
                    end try
                end repeat
                if cur2 is targetFinal then exit repeat
                delay 0.5
            end repeat
            set finalState to cur2
            if finalState is not targetFinal then error "did not reach " & targetFinal
        else
            set finalState to cur
        end if
    end tell
end tell

return ("ok clicks=" & (didClick as text) & " final=" & finalState)
