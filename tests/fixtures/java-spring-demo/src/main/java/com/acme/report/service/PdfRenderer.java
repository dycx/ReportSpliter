package com.acme.report.service;

import com.acme.report.model.Report;
import org.springframework.stereotype.Component;

@Component
public class PdfRenderer {

    public Report render(Report report, String format) {
        if ("pdf".equalsIgnoreCase(format)) {
            report.setFormat("pdf");
        }
        return report;
    }
}

