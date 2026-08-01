package com.acme.report.repo;

import com.acme.report.model.Report;
import org.springframework.data.jpa.repository.JpaRepository;

import java.time.LocalDate;
import java.util.List;

public interface ReportRepository extends JpaRepository<Report, Long> {

    Report findByDate(LocalDate date);

    List<Report> findByDateBetween(LocalDate start, LocalDate end);
}

