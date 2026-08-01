package com.acme.report.support;

import java.time.LocalDate;
import java.time.LocalDateTime;

public final class DateUtils {

    private DateUtils() {
    }

    public static LocalDate parse(String date) {
        return LocalDate.parse(date);
    }

    public static LocalDateTime now() {
        return LocalDateTime.now();
    }
}

