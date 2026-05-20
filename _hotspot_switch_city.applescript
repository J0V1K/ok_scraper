-- Switch Hotspot Shield to a specific US city via the in-app picker.
-- Usage: osascript _hotspot_switch_city.applescript "Boston"
--
-- The target city must already be visible in the picker (Selected location,
-- Quick access, or expanded country list). For the bootstrap rotation pool
-- we use cities already in the Quick access list (Atlanta / Boston / Charlotte
-- on the calibration profile).
--
-- Output on success: "ok target=<city> rows-scanned=<n>"

on run argv
    if (count of argv) = 0 then error "missing city argument"
    set targetCity to item 1 of argv

    tell application "Hotspot Shield" to activate
    delay 0.6

    tell application "System Events"
        tell process "Hotspot Shield"
            -- Open the location picker by clicking the change-location button
            -- (button 1 of split group), unless the picker is already open.
            set pickerOpen to false
            try
                set _t to table 1 of scroll area 1 of UI element 3 of UI element 1 of window 1
                if (count of rows of _t) > 0 then set pickerOpen to true
            end try
            if not pickerOpen then
                click button 1 of UI element 1 of window 1
                delay 1.2
            end if

            set t to table 1 of scroll area 1 of UI element 3 of UI element 1 of window 1
            set rowCount to (count of rows of t)
            set matchIdx to 0
            set i to 0
            repeat with r in rows of t
                set i to i + 1
                if i > rowCount then exit repeat
                try
                    set ec to entire contents of r
                    set hasUS to false
                    set hasCity to false
                    repeat with x in ec
                        try
                            set vv to value of x as text
                            if vv is "United States" then set hasUS to true
                            if vv is targetCity then set hasCity to true
                        end try
                    end repeat
                    if hasUS and hasCity then
                        set matchIdx to i
                        exit repeat
                    end if
                end try
            end repeat

            if matchIdx = 0 then
                error "row for 'United States / " & targetCity & "' not found"
            end if

            -- Click the AXButton inside that row to select + connect.
            tell row matchIdx of t
                set ec to entire contents
                set clicked to false
                repeat with x in ec
                    try
                        if (role of x) is "AXButton" then
                            click x
                            set clicked to true
                            exit repeat
                        end if
                    end try
                end repeat
                if not clicked then error "no AXButton inside matching row"
            end tell

            -- Wait for the dashboard to come back with the Disconnect button
            -- (= connected to the new city).
            set deadline to (current date) + 75
            set cur to ""
            repeat while (current date) < deadline
                try
                    repeat with el in (every UI element of UI element 1 of window 1)
                        try
                            set d to description of el
                            if d is "Disconnect button" then
                                set cur to "Disconnect button"
                                exit repeat
                            end if
                        end try
                    end repeat
                end try
                if cur is "Disconnect button" then exit repeat
                delay 0.5
            end repeat
            if cur is not "Disconnect button" then
                error "did not reach connected state for " & targetCity
            end if
        end tell
    end tell

    return ("ok target=" & targetCity & " rows-scanned=" & rowCount)
end run
