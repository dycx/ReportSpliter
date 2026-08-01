package com.acme.report.service;

import com.acme.report.model.Report;
import org.springframework.stereotype.Service;

@Service
public class ReportCalculationService {

    public Report compute(Report report) {
        report.setTotal(report.getAmount() * 1.08);
        report.setStatus("COMPUTED");
        return report;
    }
}

